"""Managing other Venvy servers from this one.

A remote server is another Venvy plus an API token made on it (Account →
API tokens, scope full for control, read for watching). This Venvy keeps the
token encrypted and talks to the other one's API: its apps show up here with
their state, and start / stop / restart / deploy go through to it. Every
request carries that server's own token, so the other server's roles, app
access and audit log apply as usual.
"""
import asyncio
import json
import re
from typing import List, Optional

import aiohttp
from sqlmodel import Session, select

from venvy.config import HTTP_CLIENT_LIMITS, utc_now
from venvy.crypto import _decrypt_secret, _encrypt_secret
from venvy.db import engine
from venvy.models import RemoteServer

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._\-]{0,40}")
ACTIONS = ("start", "stop", "restart")


def clean_url(url: str) -> str:
    from venvy.notify import _check_safe_outbound_url
    u = (url or "").strip().rstrip("/")
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~\-/]*)?", u):
        raise ValueError("The URL must look like https://venvy.example.com")
    _check_safe_outbound_url(u, "url")
    return u


async def _call(server: RemoteServer, method: str, path: str, body: Optional[dict] = None):
    token = _decrypt_secret(server.token)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), **HTTP_CLIENT_LIMITS) as http:
        async with http.request(method, server.url + path, json=body,
                                headers={"Authorization": f"Bearer {token}"}) as r:
            text = await r.text()
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = {"detail": text[:300]}
            if r.status >= 400:
                detail = data.get("detail") if isinstance(data, dict) else None
                raise RuntimeError(f"{server.name}: HTTP {r.status} {detail or ''}".strip())
            return data


async def check(server_id: int) -> dict:
    with Session(engine) as s:
        srv = s.get(RemoteServer, server_id)
    if srv is None:
        raise ValueError("No such server.")
    try:
        info = await _call(srv, "GET", "/api/system/info")
        status = "ok"
    except Exception as exc:
        info, status = None, str(exc)[:200]
    with Session(engine) as s:
        row = s.get(RemoteServer, server_id)
        if row:
            row.last_status = status
            if status == "ok":
                row.last_seen = utc_now()
            s.add(row)
            s.commit()
    return {"status": status, "info": info}


async def all_apps() -> List[dict]:
    with Session(engine) as s:
        servers = s.exec(select(RemoteServer)).all()

    async def one(srv: RemoteServer):
        try:
            apps = await _call(srv, "GET", "/api/apps")
            return [{"server_id": srv.id, "server": srv.name, "id": a.get("id"), "name": a.get("name"),
                     "status": a.get("status"), "runtime": a.get("runtime"), "domain": a.get("domain"),
                     "access_url": a.get("access_url"), "last_deploy_status": a.get("last_deploy_status")}
                    for a in (apps or []) if isinstance(a, dict) and not a.get("parent_app_id")]
        except Exception as exc:
            return [{"server_id": srv.id, "server": srv.name, "error": str(exc)[:200]}]
    results = await asyncio.gather(*(one(s) for s in servers))
    return [x for lst in results for x in lst]


async def control(server_id: int, app_id: int, action: str) -> dict:
    if action not in ACTIONS + ("deploy",):
        raise ValueError("Unknown action.")
    with Session(engine) as s:
        srv = s.get(RemoteServer, server_id)
    if srv is None:
        raise ValueError("No such server.")
    if action == "deploy":
        return await _call(srv, "POST", f"/api/apps/{int(app_id)}/deploy", {"start": False})
    return await _call(srv, "POST", f"/api/apps/{int(app_id)}/control?action={action}")


def encrypt_token(token: str) -> str:
    if not token or len(token) > 500 or any(c.isspace() for c in token):
        raise ValueError("Paste the API token made on the other server.")
    return _encrypt_secret(token)
