"""Secrets beyond environment variables.

* Secret files: named files (a TLS key, a credentials.json) stored encrypted
  with the app and written, at every start, into $SECRETS_DIR — a folder the
  app's user can read but not change (root-owned, its group).
* SOPS: a file in the app's repository encrypted with SOPS (age); decrypted
  by Venvy at start with the age key kept in Venvy's settings, its top-level
  values become environment variables. The key never reaches the app.
* HashiCorp Vault: a KV v2 path read at start with Venvy's Vault token; its
  keys become environment variables.

Values from SOPS and Vault win over the app's own variables.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Dict, Optional

import aiohttp

from venvy import appspec, kv
from venvy.config import HTTP_CLIENT_LIMITS
from venvy.crypto import _encrypt_secret, _try_decrypt_secret

FILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,99}")
MAX_FILES = 50
MAX_FILE_BYTES = 256 * 1024
VAULT_KEY = "vault"          # {"addr": ..., "token": ..., "namespace": ...}, encrypted
SOPS_KEY = "sops_age_key"    # encrypted
_VAULT_PATH_RE = re.compile(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_.\-]+)+")


def load_files(app) -> Dict[str, str]:
    raw = getattr(app, "secret_files", "") or ""
    if not raw:
        return {}
    ok, plain = _try_decrypt_secret(raw)
    if not ok:
        raise RuntimeError("The app's secret files can't be decrypted (the key was replaced).")
    try:
        data = json.loads(plain or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def clean_files(files: Dict[str, str]) -> str:
    if len(files) > MAX_FILES:
        raise ValueError(f"At most {MAX_FILES} secret files.")
    for name, content in files.items():
        if not FILE_NAME_RE.fullmatch(name) or name in (".", ".."):
            raise ValueError(f"'{name}' is not a usable file name (letters, digits, . - _).")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(f"{name} is larger than {MAX_FILE_BYTES // 1024} KB.")
    return _encrypt_secret(json.dumps(files)) if files else ""


def write_files(app, ids: Optional[tuple]) -> None:
    """Put the app's secret files in $SECRETS_DIR (and remove ones it no longer has)."""
    files = load_files(app)
    d = appspec.secrets_dir(app)
    if not d.is_dir():
        if not files:
            return
        appspec.ensure_state_dirs(app, ids)
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in os.listdir(dfd):
            if name not in files:
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=dfd)
        for name, content in files.items():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name, dir_fd=dfd)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440 if ids else 0o600, dir_fd=dfd)
            try:
                os.write(fd, content.encode("utf-8"))
                if ids:
                    os.fchown(fd, 0, ids[1])
            finally:
                os.close(fd)
    finally:
        os.close(dfd)


def _flatten(data) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if k == "sops" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k)):
            continue
        out[str(k)] = v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    return out


def _read_dotenv(text: str) -> Dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            out[k] = v
    return out


async def sops_env(app) -> Dict[str, str]:
    rel = getattr(app, "sops_file", None)
    if not rel:
        return {}
    sops = shutil.which("sops")
    if not sops:
        raise RuntimeError("sops is not installed on this host (installer: --with-sops).")
    key = kv.get(SOPS_KEY, "")
    ok, age_key = _try_decrypt_secret(key) if key else (False, "")
    if not ok or not age_key:
        raise RuntimeError("No SOPS age key is set (System → Secrets).")
    from venvy.safefs import AppPathError, _fs_open_file
    wd_rel = appspec.clean_rel_path(getattr(app, "root_dir", "") or "")
    parts = tuple(p for p in (wd_rel + "/" + rel).split("/") if p)
    try:
        f, _ = _fs_open_file(Path(app.app_dir), parts)
    except AppPathError as exc:
        raise RuntimeError(f"The SOPS file {rel} can't be read: {exc}")
    with f:
        content = f.read(2 * 1024 * 1024)
    ext = Path(rel).suffix.lower()
    in_type = {".yaml": "yaml", ".yml": "yaml", ".json": "json", ".env": "dotenv", ".ini": "ini"}.get(ext, "binary")
    out_type = "dotenv" if in_type == "dotenv" else "json"
    proc = await asyncio.create_subprocess_exec(
        sops, "--decrypt", "--input-type", in_type, "--output-type", out_type, "/dev/stdin",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "SOPS_AGE_KEY": age_key, "HOME": "/nonexistent"})
    try:
        out, err = await asyncio.wait_for(proc.communicate(content), timeout=60)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise RuntimeError("sops timed out.")
    if proc.returncode != 0:
        raise RuntimeError("sops could not decrypt " + rel + ": " + err.decode(errors="replace").strip()[-300:])
    text = out.decode("utf-8", errors="replace")
    if out_type == "dotenv":
        return _read_dotenv(text)
    try:
        return _flatten(json.loads(text))
    except ValueError:
        raise RuntimeError(f"sops did not return a map of values for {rel}.")


def clean_vault_path(raw: Optional[str]) -> Optional[str]:
    p = (raw or "").strip().strip("/")
    if not p:
        return None
    if not _VAULT_PATH_RE.fullmatch(p) or ".." in p:
        raise ValueError("The Vault path must look like secret/myapp (mount/path).")
    return p


async def vault_env(app) -> Dict[str, str]:
    path = getattr(app, "vault_path", None)
    if not path:
        return {}
    cfg = kv.get_json(VAULT_KEY, None, secret=True)
    if not cfg or not cfg.get("addr") or not cfg.get("token"):
        raise RuntimeError("Vault is not configured (System → Secrets).")
    mount, _, rest = path.partition("/")
    url = f"{cfg['addr'].rstrip('/')}/v1/{mount}/data/{rest}"
    headers = {"X-Vault-Token": cfg["token"]}
    if cfg.get("namespace"):
        headers["X-Vault-Namespace"] = cfg["namespace"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), **HTTP_CLIENT_LIMITS) as http:
        async with http.get(url, headers=headers) as r:
            if r.status != 200:
                raise RuntimeError(f"Vault answered HTTP {r.status} for {path}.")
            body = await r.json(content_type=None)
    data = ((body or {}).get("data") or {}).get("data")
    return _flatten(data)


async def resolve(app, log: Callable[[str], None] = lambda m: None) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if getattr(app, "sops_file", None):
        env.update(await sops_env(app))
        log(f"Loaded {len(env)} values from {app.sops_file} (SOPS).")
    if getattr(app, "vault_path", None):
        v = await vault_env(app)
        env.update(v)
        log(f"Loaded {len(v)} values from Vault ({app.vault_path}).")
    return env
