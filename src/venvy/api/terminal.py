"""The terminal WebSocket endpoint.

Registered directly on the application rather than on ``api_router``: WebSocket
routes do not run the HTTP middleware stack, so authentication is performed
explicitly here against the same session rules the HTTP dependencies use.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from sqlmodel import Session

from venvy.db.engine import get_session
from venvy.db.models import require_id
from venvy.security.dependencies import authenticate_session_token
from venvy.services.terminal import terminal_manager

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: RFC 6455 close code for a policy violation.
_WS_POLICY_VIOLATION = 1008


@router.websocket("/api/apps/{app_id}/terminal")
async def terminal_ws(
    websocket: WebSocket,
    app_id: int,
    session: Session = Depends(get_session),
) -> None:
    """Attach an interactive shell for *app_id* to this WebSocket.

    The handshake is accepted before authentication so the client receives a
    close frame carrying a readable reason, rather than an opaque HTTP error it
    cannot surface to the user.
    """
    await websocket.accept()

    try:
        user = authenticate_session_token(websocket.cookies.get("session"), session)
    except HTTPException as exc:
        await websocket.close(code=_WS_POLICY_VIOLATION, reason=str(exc.detail))
        return

    try:
        await terminal_manager.start_terminal(
            websocket, app_id, session, require_id(user.id), user.role
        )
    except Exception:
        logger.exception("Terminal session for app %s failed", app_id)
        try:
            await websocket.close()
        except Exception:
            pass
