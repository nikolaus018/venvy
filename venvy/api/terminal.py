"""The terminal WebSocket endpoint."""
import asyncio
import json
import logging
import urllib.parse

from fastapi import APIRouter, HTTPException, WebSocket
from sqlmodel import Session

from venvy.audit import audit_log
from venvy.auth import _authenticate, check_ip_allowlist, has_app_access, request_is_https
from venvy.config import _ALLOWED_ORIGINS
from venvy.db import engine
from venvy.models import App
from venvy.terminal import terminal_manager

router = APIRouter()

_PREAUTH_SEM = asyncio.Semaphore(32)


@router.websocket("/api/apps/{app_id}/terminal")
async def terminal_ws(
    websocket: WebSocket,
    app_id: int,
):
    # No Depends(get_session) here: a dependency session lives as long as the
    # socket, and after its first query it sits in an open read transaction.
    # Every open terminal then pinned one pooled connection (15 terminals
    # exhausted the pool and hung the whole API) and kept SQLite from
    # checkpointing its WAL. Sessions below are opened and closed per lookup.
    #
    # Reject disallowed IPs and cross-origin handshakes BEFORE accept() so
    # unauthenticated clients cannot hold open upgraded WebSocket connections.
    try:
        check_ip_allowlist(websocket)
    except HTTPException:
        await websocket.close(code=1008, reason="Access from this IP is not allowed.")
        return

    origin = websocket.headers.get("origin", "")
    host = (websocket.headers.get("host", "") or "").lower()
    parsed = urllib.parse.urlsplit(origin) if origin else None
    origin_host = (parsed.netloc or "").lower() if parsed else ""
    origin_scheme = (parsed.scheme or "").lower() if parsed else ""
    allowed_origins_lower = {o.lower() for o in _ALLOWED_ORIGINS}
    same_origin = bool(origin_host and origin_host == host and origin_scheme in ("http", "https"))
    if same_origin and request_is_https(websocket) and origin_scheme != "https":
        same_origin = False
    if not same_origin and origin.lower() not in allowed_origins_lower:
        await websocket.close(code=1008, reason="Cross-origin terminal connections are not allowed.")
        return

    try:
        await asyncio.wait_for(_PREAUTH_SEM.acquire(), timeout=1.0)
    except asyncio.TimeoutError:
        await websocket.close(code=1013, reason="Server busy.")
        return

    try:
        await websocket.accept()

        # Browsers can't put custom headers on a WebSocket handshake, so the page
        # sends its X-Venvy-Key as the first message: "\x01" + key.
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        except Exception:
            await websocket.close(code=1008, reason="Not authenticated.")
            return
        auth_key = first[1:] if first.startswith("\x01") else None
        cookie_token = websocket.cookies.get("session")

        def _auth_check():
            with Session(engine) as session:
                ws_user = _authenticate(session, cookie_token, auth_key)
                if not ws_user:
                    return None, None, None
                if not has_app_access(session, ws_user, app_id):
                    return None, None, None
                return ws_user.id, ws_user.role, ws_user.username

        ws_user_id, ws_user_role, ws_username = await asyncio.to_thread(_auth_check)
        if ws_user_id is None:
            await websocket.close(code=1008, reason="Not authenticated.")
            return
        if ws_user_role not in ("admin", "operator"):
            await websocket.close(code=1008, reason="Operator or admin privileges required.")
            return
    finally:
        _PREAUTH_SEM.release()

    # The page sends its size straight after the key. Waiting a moment for it
    # lets the shell start at that size: started at the default 220 columns
    # and resized a moment later, a phone got its first prompt garbled.
    size, first_input = None, None
    try:
        nxt = await asyncio.wait_for(websocket.receive_text(), timeout=0.5)
    except asyncio.TimeoutError:
        nxt = None
    except Exception:
        return
    if nxt and nxt.startswith("\x00"):
        try:
            msg = json.loads(nxt[1:])
            size = (int(msg["rows"]), int(msg["cols"]))
        except (ValueError, KeyError, TypeError):
            pass
    elif nxt:
        first_input = nxt

    def _still_allowed() -> bool:
        with Session(engine) as s:
            u = _authenticate(s, cookie_token, auth_key)
            return bool(u and u.role in ("admin", "operator") and s.get(App, app_id) and has_app_access(s, u, app_id))

    audit_log(ws_username, "terminal_open", f"app id={app_id}")
    try:
        await terminal_manager.start_terminal(
            websocket, app_id, ws_user_id, ws_user_role, _still_allowed, size=size, first_input=first_input
        )
    except Exception as exc:
        logging.error(f"WS terminal error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass
