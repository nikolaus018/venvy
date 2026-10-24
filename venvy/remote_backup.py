"""Copying a backup off this machine: S3-compatible storage, WebDAV, or scp.

The settings are one encrypted JSON object (system setting `backup_remote`)."""
import asyncio
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict, Optional

import aiohttp
from sqlmodel import Session

from venvy.config import HTTP_CLIENT_LIMITS, DATA_DIR
from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.models import SystemSetting

KEY = "backup_remote"
KINDS = ("s3", "webdav", "scp")
MASK = "***"
SECRET_FIELDS = {"s3": ("secret_key",), "webdav": ("password",), "scp": ("private_key",)}
REQUIRED = {"s3": ("bucket", "access_key", "secret_key"), "webdav": ("url",), "scp": ("host", "user", "path", "private_key")}
S3_MAX_BYTES = 5 * 1024 ** 3


class RemoteError(Exception):
    pass


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load() -> Optional[dict]:
    with Session(engine) as s:
        row = s.get(SystemSetting, KEY)
    if not row or not row.value:
        return None
    ok, plain = _try_decrypt_secret(row.value)
    if not ok or not plain:
        return None
    try:
        data = json.loads(plain)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("kind") in KINDS else None


def save(cfg: Optional[dict]) -> None:
    with Session(engine) as s:
        row = s.get(SystemSetting, KEY) or SystemSetting(key=KEY)
        row.value = _encrypt_secret(json.dumps(cfg)) if cfg else ""
        s.add(row)
        s.commit()


def clean(cfg: dict, old: Optional[dict]) -> dict:
    """Validated settings; a secret sent back as *** keeps its stored value."""
    kind = cfg.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    out = {k: (v.strip() if isinstance(v, str) and k != "private_key" else v) for k, v in cfg.items() if v is not None}
    for f in SECRET_FIELDS[kind]:
        if out.get(f) == MASK:
            if old and old.get("kind") == kind and old.get(f):
                out[f] = old[f]
            else:
                out.pop(f, None)
    for f in REQUIRED[kind]:
        if not out.get(f):
            raise ValueError(f"{f} is required.")
    if kind == "webdav" and not out["url"].startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    if kind == "s3" and out.get("endpoint") and not out["endpoint"].startswith(("http://", "https://")):
        raise ValueError("endpoint must start with http:// or https://")
    if kind == "scp":
        if not str(out.get("private_key", "")).lstrip().startswith("-----BEGIN"):
            raise ValueError("private_key must be an SSH private key (starts with -----BEGIN).")
        out["private_key"] = out["private_key"].strip() + "\n"
        try:
            out["port"] = int(out.get("port") or 22)
        except (TypeError, ValueError):
            raise ValueError("port must be a number.")
        for f in ("host", "user", "path"):
            if any(c in out[f] for c in " \t\r\n\0'\"`$;|&<>\\") or out[f].startswith("-"):
                raise ValueError(f"{f} contains characters that are not allowed.")
    return out


def masked(cfg: Optional[dict]) -> Optional[dict]:
    if not cfg:
        return None
    sec = SECRET_FIELDS[cfg["kind"]]
    return {k: (MASK if k in sec and v else v) for k, v in cfg.items()}


# ---------------------------------------------------------------------------
# AWS Signature V4
# ---------------------------------------------------------------------------

def sigv4_headers(method: str, url: str, region: str, service: str, access_key: str, secret_key: str,
                  payload_hash: str, headers: Dict[str, str], now: datetime.datetime) -> Dict[str, str]:
    """Headers to add to a request so S3 accepts it. *headers* are the ones sent
    anyway that are to be signed (Host is derived from the URL)."""
    u = urllib.parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    stamp = now.strftime("%Y%m%d")
    all_headers = {k.lower(): " ".join(v.split()) for k, v in headers.items()}
    all_headers["host"] = u.netloc
    all_headers["x-amz-content-sha256"] = payload_hash
    all_headers["x-amz-date"] = amz_date
    names = sorted(all_headers)
    canonical_headers = "".join(f"{n}:{all_headers[n]}\n" for n in names)
    signed = ";".join(names)
    path = urllib.parse.quote(urllib.parse.unquote(u.path) or "/", safe="/~")
    query = "&".join(f"{urllib.parse.quote(k, safe='~')}={urllib.parse.quote(v, safe='~')}"
                     for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([method, path, query, canonical_headers, signed, payload_hash])
    scope = f"{stamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])

    def h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()
    k = h(h(h(h(("AWS4" + secret_key).encode(), stamp), region), service), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}",
    }


def _s3_url(cfg: dict, key: str) -> str:
    region = cfg.get("region") or "us-east-1"
    quoted = urllib.parse.quote(key, safe="/~")
    endpoint = (cfg.get("endpoint") or "").rstrip("/")
    if endpoint:
        # Custom endpoints (MinIO, Backblaze, Wasabi, ...) are addressed by path.
        return f"{endpoint}/{cfg['bucket']}/{quoted}"
    host = "s3.amazonaws.com" if region == "us-east-1" else f"s3.{region}.amazonaws.com"
    return f"https://{cfg['bucket']}.{host}/{quoted}"


def _object_key(cfg: dict, name: str) -> str:
    prefix = (cfg.get("prefix") or "").strip("/")
    return f"{prefix}/{name}" if prefix else name


async def _file_chunks(path: Path):
    with open(path, "rb") as f:
        while True:
            chunk = await asyncio.to_thread(f.read, 1 << 20)
            if not chunk:
                return
            yield chunk


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------

async def upload(path: Path, cfg: Optional[dict] = None, name: Optional[str] = None) -> str:
    """Copy *path* to the configured place; returns where it went. Raises RemoteError."""
    cfg = cfg or load()
    if not cfg:
        raise RemoteError("No off-site copy is set up.")
    name = name or path.name
    size = path.stat().st_size
    kind = cfg["kind"]
    try:
        if kind == "s3":
            return await _upload_s3(path, size, cfg, name)
        if kind == "webdav":
            return await _upload_webdav(path, size, cfg, name)
        return await _upload_scp(path, cfg, name)
    except RemoteError:
        raise
    except Exception as exc:
        raise RemoteError(str(exc)[:300] or exc.__class__.__name__)


async def _upload_s3(path: Path, size: int, cfg: dict, name: str) -> str:
    if size > S3_MAX_BYTES:
        raise RemoteError("The backup is over 5 GB, the most a single S3 upload takes.")
    url = _s3_url(cfg, _object_key(cfg, name))
    # The file is read twice: once to hash (SigV4 over plain HTTP wants the real hash), once to send.
    h = hashlib.sha256()
    async for chunk in _file_chunks(path):
        h.update(chunk)
    signed = sigv4_headers("PUT", url, cfg.get("region") or "us-east-1", "s3", cfg["access_key"], cfg["secret_key"],
                           h.hexdigest(), {}, datetime.datetime.now(datetime.timezone.utc))
    headers = {**signed, "Content-Type": "application/gzip", "Content-Length": str(size)}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
    async with aiohttp.ClientSession(timeout=timeout, **HTTP_CLIENT_LIMITS) as http:
        async with http.put(url, data=_file_chunks(path), headers=headers) as resp:
            if resp.status >= 300:
                raise RemoteError(f"S3 answered {resp.status}: {(await resp.text())[:300]}")
    return f"s3://{cfg['bucket']}/{_object_key(cfg, name)}"


async def _upload_webdav(path: Path, size: int, cfg: dict, name: str) -> str:
    url = cfg["url"].rstrip("/") + "/" + urllib.parse.quote(name)
    auth = aiohttp.BasicAuth(cfg.get("username", ""), cfg.get("password", "")) if cfg.get("username") else None
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600)
    async with aiohttp.ClientSession(timeout=timeout, auth=auth, **HTTP_CLIENT_LIMITS) as http:
        async with http.put(url, data=_file_chunks(path), headers={"Content-Length": str(size)}) as resp:
            if resp.status >= 300:
                raise RemoteError(f"The WebDAV server answered {resp.status}: {(await resp.text())[:300]}")
    return url


async def _upload_scp(path: Path, cfg: dict, name: str) -> str:
    fd, key_path = tempfile.mkstemp(prefix="venvy-scp-", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(cfg["private_key"])
        os.chmod(key_path, 0o600)
        known = DATA_DIR / ".known_hosts_backup"
        dest = f"{cfg['user']}@{cfg['host']}:{cfg['path'].rstrip('/')}/{name}"
        proc = await asyncio.create_subprocess_exec(
            "scp", "-q", "-i", key_path, "-P", str(cfg.get("port", 22)), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={known}", "--", str(path), dest,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3600)
        except asyncio.TimeoutError:
            proc.kill()
            raise RemoteError("scp timed out after an hour.")
        if proc.returncode != 0:
            raise RemoteError(f"scp failed: {out.decode(errors='replace').strip()[:300]}")
    except FileNotFoundError:
        raise RemoteError("scp is not installed on this host (apt-get install openssh-client).")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(key_path)
    return dest


async def test(cfg: dict) -> str:
    """Upload a few bytes to prove the settings work."""
    fd, tmp = tempfile.mkstemp(prefix="venvy-test-", dir=str(DATA_DIR))
    try:
        with os.fdopen(fd, "w") as f:
            f.write("Venvy off-site backup test\n")
        return await upload(Path(tmp), cfg, name="venvy-test.txt")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
