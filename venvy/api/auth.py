"""Authentication routes: setup, login/logout, password, 2FA, download tickets."""
import secrets
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import or_, update
from sqlmodel import select, Session

from venvy.audit import audit_log
from venvy.auth import (
    _authenticate,
    check_ip_allowlist,
    _check_login_rate,
    _clean_username,
    _clear_login_rate,
    _DOWNLOAD_TICKET_TTL,
    _download_tickets,
    _download_tickets_lock,
    _DUMMY_PASSWORD_HASH,
    get_current_user,
    hash_password,
    _hash_token,
    _login_gate,
    _LOGIN_MAX_TRIES_PER_USER,
    _login_rate_key,
    _login_user_key,
    _new_session,
    _record_login_failure,
    _totp_generate_secret,
    _totp_uri,
    _totp_verify,
    _validate_password_strength,
    verify_password,
)
from venvy.crypto import _decrypt_secret, _encrypt_secret
from venvy.db import get_session
from venvy.models import SystemSetting, User, UserSession

router = APIRouter(
    prefix="/api/auth", tags=["auth"], dependencies=[Depends(check_ip_allowlist)]
)


class UserReq(BaseModel):
    username: str
    password: str
    role: Optional[str] = None


class PwdReq(BaseModel):
    current_password: str
    password: str
    password_confirm: str


class TotpSetupReq(BaseModel):
    totp_code: str


class TotpDisableReq(BaseModel):
    password: str


class UserLoginReq(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


@router.get("/status")
def auth_status(request: Request, session: Session = Depends(get_session)):
    user_exists = session.exec(select(User)).first() is not None
    user = _authenticate(session, request.cookies.get("session"), request.headers.get("x-venvy-key"))
    return {
        "user_exists": user_exists,
        "authenticated": user is not None,
        "username": user.username if user else None,
        "role": user.role if user else None,
        "totp_enabled": user.totp_enabled if user else False,
    }


_setup_lock = threading.Lock()


@router.post("/setup")
def auth_setup(payload: UserReq, request: Request, response: Response, session: Session = Depends(get_session)):
    username = _clean_username(payload.username)
    _validate_password_strength(payload.password)
    password_hash = hash_password(payload.password)
    # Handlers run in a thread pool: without the lock, two first-run requests
    # could both see an empty user table and each create an admin account.
    with _setup_lock:
        if session.exec(select(User)).first():
            raise HTTPException(400, "Setup already completed.")
        user = User(username=username, password_hash=password_hash, role="admin")
        session.add(user)
        session.commit()
    session.refresh(user)
    auth_key = _new_session(session, response, user)
    audit_log(user.username, "user_setup", "initial admin account created")
    return {"ok": True, "username": user.username, "role": user.role, "auth_key": auth_key}


@router.post("/login")
def login(
    payload: UserLoginReq,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    # Usernames are stored trimmed (see _clean_username); a trailing space
    # from a phone keyboard must not read as "Invalid credentials".
    username = payload.username.strip()
    rate_key = _login_rate_key(request)
    user_key = _login_user_key(username)
    _check_login_rate(rate_key)
    _check_login_rate(user_key)

    def _fail(status: int, detail: str):
        _record_login_failure(rate_key)
        _record_login_failure(user_key, _LOGIN_MAX_TRIES_PER_USER)
        _check_login_rate(rate_key)   # answers 429 if that was the last allowed try
        _check_login_rate(user_key)
        raise HTTPException(status, detail)

    user = session.exec(select(User).where(User.username == username)).first()
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        _fail(401, "Invalid credentials.")

    if user.totp_enabled and user.totp_secret:
        if not payload.totp_code:
            raise HTTPException(403, "totp_required")
        step = _totp_verify(_decrypt_secret(user.totp_secret), payload.totp_code, after=user.totp_last_counter)
        if step is None:
            _fail(401, "Invalid authenticator code.")
        # Claim the step in one statement: two logins presenting the same
        # code at the same moment both pass the check above, and only the
        # first may win. Committed with the new session below.
        claimed = session.exec(
            update(User)
            .where(User.id == user.id,
                   or_(User.totp_last_counter.is_(None), User.totp_last_counter < step))
            .values(totp_last_counter=step)
        )
        if claimed.rowcount != 1:
            _fail(401, "Invalid authenticator code.")

    _clear_login_rate(user_key)
    _clear_login_rate(rate_key)
    auth_key = _new_session(session, response, user)
    audit_log(user.username, "login", f"from {rate_key}")
    return {"ok": True, "username": user.username, "role": user.role, "auth_key": auth_key}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    token = request.cookies.get("session")
    if token:
        db_session = session.get(UserSession, _hash_token(token))
        if db_session:
            session.delete(db_session)
            session.commit()
    response.delete_cookie("session", path="/")
    return {"ok": True}


@router.put("/password")
def change_own_password(
    req: PwdReq,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    wrong_password = _login_gate(f"pwd:{user.id}")
    if not verify_password(req.current_password, user.password_hash):
        wrong_password()
        raise HTTPException(401, "Current password is incorrect.")
    _clear_login_rate(f"pwd:{user.id}")
    if req.password != req.password_confirm:
        raise HTTPException(400, "New passwords do not match.")
    _validate_password_strength(req.password)
    user.password_hash = hash_password(req.password)
    session.add(user)
    # A password change is usually "someone may know the old one" — sign out
    # every other session of this user, keep only the one making the change.
    current = _hash_token(request.cookies.get("session") or "")
    for row in session.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        if row.token != current:
            session.delete(row)
    session.commit()
    audit_log(user.username, "password_change", "other sessions revoked")
    return {"ok": True}


@router.post("/download-ticket")
def create_download_ticket(user: User = Depends(get_current_user)):
    """Trade the X-Venvy-Key header for a one-time, 60-second ticket usable as
    ?ticket= on a download URL (see get_download_user)."""
    ticket = secrets.token_urlsafe(24)
    with _download_tickets_lock:
        _download_tickets[_hash_token(ticket)] = (user.id, time.monotonic() + _DOWNLOAD_TICKET_TTL)
    return {"ticket": ticket}


# ---------------------------------------------------------------------------
# TOTP routes
# ---------------------------------------------------------------------------

@router.get("/totp/setup")
def totp_setup_begin(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Generate (or return pending) TOTP secret + QR URI. Does NOT enable TOTP yet."""
    if user.totp_enabled:
        # Replacing an active authenticator must go through /totp/disable,
        # which asks for the password — a borrowed session alone must not be
        # able to re-enrol 2FA onto someone else's phone.
        raise HTTPException(400, "Two-factor authentication is already enabled. Disable it first to set up a new authenticator.")
    secret = _totp_generate_secret()
    key = f"totp_pending_{user.id}"
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = _encrypt_secret(secret)
    else:
        setting = SystemSetting(key=key, value=_encrypt_secret(secret))
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
    secret = _decrypt_secret(setting.value)
    step = _totp_verify(secret, req.totp_code)
    if step is None:
        raise HTTPException(400, "Incorrect code — try again.")
    user.totp_secret = _encrypt_secret(secret)
    user.totp_enabled = True
    user.totp_last_counter = step
    session.add(user)
    session.delete(setting)
    session.commit()
    audit_log(user.username, "totp_enable")
    return {"ok": True}


@router.post("/totp/disable")
def totp_disable(
    req: TotpDisableReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Disable TOTP after verifying current password."""
    wrong_password = _login_gate(f"pwd:{user.id}")
    if not verify_password(req.password, user.password_hash):
        wrong_password()
        raise HTTPException(401, "Incorrect password.")
    _clear_login_rate(f"pwd:{user.id}")
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_counter = None
    session.add(user)
    session.commit()
    audit_log(user.username, "totp_disable")
    return {"ok": True}
