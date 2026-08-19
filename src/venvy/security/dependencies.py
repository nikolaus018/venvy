"""FastAPI dependencies for identifying and authorising the caller."""

from __future__ import annotations

import datetime
import logging

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session
from starlette.requests import HTTPConnection

from venvy.config import settings
from venvy.core.timeutils import make_tz_aware, utc_now
from venvy.db.engine import get_session
from venvy.db.models import User, UserSession

__all__ = [
    "authenticate_session_token",
    "check_admin",
    "check_ip_allowlist",
    "get_current_user",
    "get_optional_user",
    "require_operator",
    "resolve_client_ip",
]

logger = logging.getLogger(__name__)

#: When less than this remains on a session, its expiry is pushed out again, so
#: an actively used session never expires under the user.
_SLIDING_RENEWAL_THRESHOLD = datetime.timedelta(days=7)


def resolve_client_ip(request: Request | HTTPConnection) -> str:
    """Return the caller's real IP address.

    Venvy is designed to sit behind a Cloudflare Tunnel, so ``CF-Connecting-IP``
    is trusted: Cloudflare's edge sets that header itself and overwrites
    whatever the client sent, so it cannot be forged from outside.

    ``X-Forwarded-For`` is deliberately *not* trusted. Cloudflare only appends
    to it, so its first entry is still attacker-controlled — honouring it would
    let anyone bypass both the IP allowlist and the login lockout by setting a
    header. Without a Cloudflare header we fall back to the raw TCP peer, which
    also cannot be spoofed.
    """
    ip = (request.headers.get("cf-connecting-ip") or "").strip()
    if not ip and request.client:
        ip = (request.client.host or "").strip()
    return ip or "unknown"


def check_ip_allowlist(request: HTTPConnection) -> None:
    """Reject callers whose IP is not on the configured allowlist.

    A no-op when no allowlist is configured. An unresolvable IP is allowed
    through, since that indicates a local or Unix-socket connection rather than
    someone evading the check.
    """
    allowlist = settings.ip_allowlist
    if not allowlist:
        return
    ip = resolve_client_ip(request)
    if ip == "unknown":
        return
    if ip not in allowlist:
        logger.warning("Rejected request from non-allowlisted IP %s", ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access from this IP is not allowed.")


def authenticate_session_token(token: str | None, session: Session) -> User:
    """Resolve a session cookie to its :class:`User`, renewing the session.

    Shared by the HTTP dependency and the WebSocket handshake so both apply
    exactly the same expiry and renewal rules.

    :raises HTTPException: 401 when the token is missing, unknown or expired.
    """
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated.")

    db_session = session.get(UserSession, token)
    if not db_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session invalid.")

    now = utc_now()
    expires_at = make_tz_aware(db_session.expires_at)
    if now > expires_at:
        session.delete(db_session)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired.")

    if expires_at - now < _SLIDING_RENEWAL_THRESHOLD:
        db_session.expires_at = now + datetime.timedelta(seconds=settings.session_ttl_seconds)
        session.add(db_session)
        session.commit()

    user = session.get(User, db_session.user_id)
    if not user:
        # The account was deleted while the session was live.
        session.delete(db_session)
        session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found.")
    return user


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    """Dependency: the signed-in user, or 401."""
    return authenticate_session_token(request.cookies.get("session"), session)


def get_optional_user(request: Request, session: Session = Depends(get_session)) -> User | None:
    """Dependency: the signed-in user, or None when not signed in.

    For endpoints that describe *whether* the caller is authenticated rather
    than requiring it.
    """
    try:
        return get_current_user(request, session)
    except HTTPException as exc:
        if exc.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
            return None
        raise


def check_admin(user: User = Depends(get_current_user)) -> User:
    """Dependency: require the ``admin`` role."""
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin privileges required.")
    return user


def require_operator(user: User = Depends(get_current_user)) -> User:
    """Dependency: require a role that may mutate apps.

    Guards everything that changes state — start/stop, file edits, package
    installs, tunnel publishing. Viewers are read-only and get a 403 here.
    """
    if user.role not in ("admin", "operator"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator or admin privileges required.")
    return user
