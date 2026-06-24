import datetime
import secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from app.auth import (
    _totp_generate_secret,
    _totp_uri,
    _totp_verify,
    _validate_password_strength,
    get_current_user,
    hash_password,
    verify_password,
)
from app.config import SESSION_TTL_SECONDS, SESSION_COOKIE_SECURE, _resolve_client_ip
from app.database import App, CfRoute, SystemSetting, User, UserSession, engine, get_session, utc_now
from app.schemas import PwdReq, TotpDisableReq, TotpSetupReq, UserReq

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth routes  /  Login rate limiting
# ---------------------------------------------------------------------------
_login_attempts: dict = {}
_login_lock = threading.Lock()
_LOGIN_MAX_TRIES = 5
_LOGIN_LOCKOUT_SEC = 300


def _login_rate_key(request: Request) -> str:
    return _resolve_client_ip(request)


def _check_login_rate(key: str) -> None:
    with _login_lock:
        entry = _login_attempts.get(key)
        if not entry:
            return
        locked_until = entry.get("locked_until")
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time()) + 1
            raise HTTPException(
                429,
                f"Too many failed attempts. Try again in {remaining} seconds.",
                headers={"X-Retry-After": str(remaining)},
            )


def _record_login_failure(key: str) -> None:
    with _login_lock:
        entry = _login_attempts.setdefault(key, {"count": 0, "locked_until": None})
        entry["count"] += 1
        if entry["count"] >= _LOGIN_MAX_TRIES:
            entry["locked_until"] = time.time() + _LOGIN_LOCKOUT_SEC
            entry["count"] = 0


def _clear_login_rate(key: str) -> None:
    with _login_lock:
        _login_attempts.pop(key, None)


def _set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie with hardened flags."""
    response.set_cookie(
        "session",
        token,
        httponly=True,
        path="/",
        max_age=SESSION_TTL_SECONDS,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )


def _set_csrf_cookie(response: Response, csrf_token: str) -> None:
    """Set a non-HttpOnly CSRF token cookie for double-submit protection.
    The frontend reads this value and echoes it in the x-csrf-token header.
    """
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,  # Must be readable by JS
        path="/",
        max_age=SESSION_TTL_SECONDS,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )


class UserLoginReq(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


@router.get("/status")
def auth_status(request: Request, session: Session = Depends(get_session)):
    user_exists = session.exec(select(User)).first() is not None
    user = None
    try:
        user = get_current_user(request, session)
    except HTTPException as e:
        if e.status_code not in (401, 403):
            raise
    return {
        "user_exists": user_exists,
        "authenticated": user is not None,
        "username": user.username if user else None,
        "is_admin": user.is_admin if user else False,
        "totp_enabled": user.totp_enabled if user else False,
    }


@router.post("/setup")
def auth_setup(
    payload: UserReq,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    if session.exec(select(User)).first():
        raise HTTPException(400, "Setup already completed.")
    _validate_password_strength(payload.password)
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        is_admin=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            token=token,
            user_id=user.id,
            expires_at=utc_now() + datetime.timedelta(seconds=SESSION_TTL_SECONDS),
        )
    )
    session.commit()
    _set_session_cookie(response, token)
    _set_csrf_cookie(response, csrf_token)
    return {"ok": True, "username": user.username, "is_admin": user.is_admin}


@router.post("/login")
def login(
    payload: UserLoginReq,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    rate_key = _login_rate_key(request)
    _check_login_rate(rate_key)

    user = session.exec(select(User).where(User.username == payload.username)).first()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_login_failure(rate_key)
        entry = _login_attempts.get(rate_key, {})
        locked_until = entry.get("locked_until")
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time()) + 1
            raise HTTPException(
                429,
                f"Too many failed attempts. Try again in {remaining} seconds.",
                headers={"X-Retry-After": str(remaining)},
            )
        raise HTTPException(401, "Invalid credentials.")

    if user.totp_enabled and user.totp_secret:
        if not payload.totp_code:
            raise HTTPException(403, "totp_required")
        if not _totp_verify(user.totp_secret, payload.totp_code):
            _record_login_failure(rate_key)
            raise HTTPException(401, "Invalid authenticator code.")

    _clear_login_rate(rate_key)
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            token=token,
            user_id=user.id,
            expires_at=utc_now() + datetime.timedelta(seconds=SESSION_TTL_SECONDS),
        )
    )
    session.commit()
    _set_session_cookie(response, token)
    _set_csrf_cookie(response, csrf_token)
    return {"ok": True, "username": user.username, "is_admin": user.is_admin}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    token = request.cookies.get("session")
    if token:
        db_session = session.get(UserSession, token)
        if db_session:
            session.delete(db_session)
            session.commit()
    response.delete_cookie("session", path="/")
    response.delete_cookie("csrf_token", path="/")
    return {"ok": True}


@router.put("/password")
def change_own_password(
    req: PwdReq,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not verify_password(req.current_password, user.password_hash):
        raise HTTPException(401, "Current password is incorrect.")
    if req.password != req.password_confirm:
        raise HTTPException(400, "New passwords do not match.")
    _validate_password_strength(req.password)
    user.password_hash = hash_password(req.password)
    session.add(user)

    # Invalidate all OTHER active sessions for this user (keep current one).
    current_token = request.cookies.get("session")
    other_sessions = session.exec(
        select(UserSession).where(UserSession.user_id == user.id)
    ).all()
    for s in other_sessions:
        if s.token != current_token:
            session.delete(s)

    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# TOTP routes
# ---------------------------------------------------------------------------


@router.get("/totp/setup")
def totp_setup_begin(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Generate (or return pending) TOTP secret + QR URI. Does NOT enable TOTP yet."""
    secret = _totp_generate_secret()
    key = f"totp_pending_{user.id}"
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = secret
    else:
        setting = SystemSetting(key=key, value=secret)
    session.add(setting)
    session.commit()
    uri = _totp_uri(secret, user.username)
    return {"ok": True, "secret": secret, "uri": uri}


@router.post("/totp/setup/confirm")
def totp_setup_confirm(
    req: TotpSetupReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verify the 6-digit code against the pending secret, then enable TOTP."""
    key = f"totp_pending_{user.id}"
    setting = session.get(SystemSetting, key)
    if not setting or not setting.value:
        raise HTTPException(400, "No pending TOTP setup. Start setup first.")
    if not _totp_verify(setting.value, req.totp_code):
        raise HTTPException(400, "Incorrect code — try again.")
    user.totp_secret = setting.value
    user.totp_enabled = True
    session.add(user)
    session.delete(setting)
    session.commit()
    return {"ok": True}


@router.post("/totp/disable")
def totp_disable(
    req: TotpDisableReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Disable TOTP after verifying current password."""
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Incorrect password.")
    user.totp_enabled = False
    user.totp_secret = None
    session.add(user)
    session.commit()
    return {"ok": True}
