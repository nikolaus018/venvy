"""The terminal WebSocket endpoint."""
import asyncio
import logging
import urllib.parse

from fastapi import APIRouter, HTTPException, WebSocket
from sqlmodel import Session

from venvy.auth import _authenticate, check_ip_allowlist
from venvy.config import _ALLOWED_ORIGINS
from venvy.db import engine
from venvy.models import App
from venvy.terminal import terminal_manager

router = APIRouter()


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
    await websocket.accept()

    # This endpoint is registered directly on `app`, not on api_router, so it
    # doesn't pick up api_router's check_ip_allowlist dependency automatically
    # — every other /api/* route is IP-gated, but a shell into an app's
    # sandbox was not. Check it explicitly, first, before any session/auth
    # logic below.
    try:
        check_ip_allowlist(websocket)
    except HTTPException:
        await websocket.close(code=1008, reason="Access from this IP is not allowed.")
        return

    # WebSockets aren't covered by CORS, and a page on any sibling port of
    # this host (i.e. a hosted app) is "same-site" — so check that the
    # handshake really comes from the Venvy UI itself.
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    origin_host = urllib.parse.urlsplit(origin).netloc if origin else ""
    if not origin_host or (origin_host != host and origin not in _ALLOWED_ORIGINS):
        await websocket.close(code=1008, reason="Cross-origin terminal connections are not allowed.")
        return

    # Browsers can't put custom headers on a WebSocket handshake, so the page
    # sends its X-Venvy-Key as the first message: "\x01" + key.
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except Exception:
        await websocket.close(code=1008, reason="Not authenticated.")
        return
    auth_key = first[1:] if first.startswith("\x01") else None
    with Session(engine) as session:
        ws_user = _authenticate(session, websocket.cookies.get("session"), auth_key)
        ws_user_id, ws_user_role = (ws_user.id, ws_user.role) if ws_user else (None, None)
    if ws_user_id is None:
        await websocket.close(code=1008, reason="Not authenticated.")
        return

    cookie_token = websocket.cookies.get("session")

    def _still_allowed() -> bool:
        with Session(engine) as s:
            u = _authenticate(s, cookie_token, auth_key)
            return bool(u and u.role in ("admin", "operator") and s.get(App, app_id))

    try:
        await terminal_manager.start_terminal(
            websocket, app_id, ws_user_id, ws_user_role, _still_allowed
        )
    except Exception as exc:
        logging.error(f"WS terminal error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass
