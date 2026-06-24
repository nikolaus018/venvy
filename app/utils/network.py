import asyncio
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Dict, Optional

import aiohttp

from fastapi import HTTPException
from pydantic import BaseModel

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None


from app.auth import *
from app.config import *
from app.database import *

# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def sanitize_slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[\s\W-]+", "-", s)
    return s or "app"


def find_free_port(start: int = 8001, end: int = 9000) -> Optional[int]:
    """Return the first TCP port in [start, end) that can be bound to."""
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return None


def _verify_port_free(port: int) -> bool:
    """Return True if the port is not currently bound — used immediately before launch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex(("localhost", port)) != 0


def is_safe_path(root: Path, target: Path) -> bool:
    """Return True only if *target* resolves to a path strictly inside (or equal to) *root*."""
    try:
        root_r = root.resolve()
        target_r = target.resolve()
        return target_r == root_r or root_r in target_r.parents
    except (ValueError, OSError):
        return False


def get_server_ipv4() -> str:
    """Return the LAN IPv4 of this machine (best-effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _build_app_env(db_app: App) -> Dict[str, str]:
    """Build the environment dict for a managed app process."""
    env = os.environ.copy()
    try:
        app_env = json.loads(db_app.env_json or "{}")
        if isinstance(app_env, dict):
            for k, v in app_env.items():
                env[str(k)] = str(v)
    except Exception:
        pass

    effective_port = None
    try:
        m_ports = json.loads(db_app.manual_ports or "[]")
        if m_ports and isinstance(m_ports, list):
            effective_port = m_ports[0]
    except Exception:
        pass

    if not effective_port:
        effective_port = db_app.port

    if effective_port:
        env["APP_PORT"] = str(effective_port)
        env["PORT"] = str(effective_port)
    env["PYTHONUNBUFFERED"] = "1"

    venv_bin = str(Path(db_app.venv_path) / "bin")
    cargo_bin = str(Path.home() / ".cargo" / "bin")

    existing_path = env.get("PATH", "")
    new_paths = [venv_bin]
    if cargo_bin not in existing_path:
        new_paths.append(cargo_bin)

    env["PATH"] = (
        ":".join(new_paths + [existing_path]) if existing_path else ":".join(new_paths)
    )

    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["VIRTUAL_ENV"] = db_app.venv_path

    for _bad in ("BASH_ENV", "ENV", "PYTHONSTARTUP"):
        env.pop(_bad, None)
    for _key in list(env.keys()):
        if "PYDEVD" in _key or "DEBUGPY" in _key:
            env.pop(_key, None)
    return {k: str(v) for k, v in env.items()}


# ---------------------------------------------------------------------------
# Cloudflare API helper
# ---------------------------------------------------------------------------


async def _cf_request(
    method: str, url: str, token: str, payload: Optional[dict] = None
) -> dict:
    """Make an asynchronous Cloudflare API call with proper timeout and error handling."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, url, headers=headers, json=payload
            ) as resp:
                data = await resp.json()
                if not (200 <= resp.status < 300):
                    errs = data.get("errors") or []
                    msg = errs[0].get("message") if errs else f"HTTP {resp.status}"
                    raise HTTPException(resp.status, f"Cloudflare API Error: {msg}")
                return data
    except asyncio.TimeoutError:
        raise HTTPException(504, "Cloudflare API request timed out (10s).")
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Cloudflare connection error: {str(exc)}")
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            500, f"Internal error during Cloudflare request: {str(exc)}"
        )


class CfPublishReq(BaseModel):
    hostname: str
    local_port: int


async def _cf_find_zone_id(
    hostname: str, account_id: str, api_token: str
) -> Optional[str]:
    """Resolve the Cloudflare Zone ID for the domain that owns *hostname*."""
    parts = hostname.split(".")
    candidates = [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
    if hostname not in candidates:
        candidates.insert(0, hostname)
    candidates = list(dict.fromkeys(candidates))
    for candidate in candidates:
        url = (
            f"https://api.cloudflare.com/client/v4/zones"
            f"?name={candidate}&account.id={account_id}&status=active"
        )
        try:
            data = await _cf_request("GET", url, api_token)
            zones = data.get("result") or []
            if zones:
                logging.info(f"CF zone found: {candidate!r} → {zones[0]['id']}")
                return zones[0]["id"]
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise HTTPException(
                    403,
                    "Cloudflare API token lacks Zone:Read permission. "
                    "Add Zone:Read and DNS:Edit to your token, then retry.",
                )
            raise
        except Exception:
            continue
    return None


async def _cf_upsert_dns_cname(
    zone_id: str,
    hostname: str,
    tunnel_id: str,
    api_token: str,
) -> str:
    """Create or update a CNAME DNS record in Cloudflare pointing to the tunnel."""
    target = f"{tunnel_id}.cfargotunnel.com"

    record_body = {
        "type": "CNAME",
        "name": hostname,
        "content": target,
        "ttl": 1,
        "proxied": True,
    }

    list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=CNAME&name={hostname}&per_page=100"
    existing = await _cf_request("GET", list_url, api_token)
    cname_records = existing.get("result") or []

    for rec_type in ("A", "AAAA"):
        conflict_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type={rec_type}&name={hostname}&per_page=100"
        try:
            conflict_data = await _cf_request("GET", conflict_url, api_token)
            for rec in conflict_data.get("result") or []:
                logging.warning(
                    f"Deleting conflicting {rec_type} record {rec['id']} for {hostname}"
                )
                await _cf_request(
                    "DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                    api_token,
                )
        except Exception as exc:
            logging.warning(f"Could not clear {rec_type} records for {hostname}: {exc}")

    if cname_records:
        for extra in cname_records[1:]:
            try:
                await _cf_request(
                    "DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{extra['id']}",
                    api_token,
                )
            except Exception as exc:
                logging.warning(
                    f"Could not delete duplicate CNAME {extra['id']}: {exc}"
                )
        record_id = cname_records[0]["id"]
        resp = await _cf_request(
            "PUT",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
            api_token,
            record_body,
        )
    else:
        resp = await _cf_request(
            "POST",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            api_token,
            record_body,
        )

    if not resp.get("success"):
        errs = resp.get("errors") or []
        err_msgs = "; ".join(e.get("message", str(e)) for e in errs)
        raise HTTPException(502, f"DNS CNAME upsert failed: {err_msgs or errs}")

    result = resp.get("result") or {}
    record_id = result.get("id", "unknown")
    logging.info(
        f"DNS CNAME upserted: {hostname} → {target} "
        f"(record {record_id}, proxied={result.get('proxied')})"
    )
    return record_id


async def _cf_delete_dns_cname(
    zone_id: str,
    hostname: str,
    api_token: str,
) -> None:
    """Delete the CNAME DNS record for *hostname* in the given zone, if it exists."""
    list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=CNAME&name={hostname}"
    try:
        existing = await _cf_request("GET", list_url, api_token)
        for rec in existing.get("result", []):
            await _cf_request(
                "DELETE",
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                api_token,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning(f"DNS record deletion failed for {hostname}: {exc}")
