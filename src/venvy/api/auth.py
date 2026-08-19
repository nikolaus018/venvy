"""Sign-in, sign-out, password changes and TOTP enrolment."""

from __future__ import annotations

import datetime
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session, select

from venvy.config import settings
from venvy.core import totp
from venvy.core.audit import audit_log
from venvy.core.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_strength,
    verify_password,
)
from venvy.core.timeutils import utc_now
from venvy.db.engine import get_session
from venvy.db.models import SystemSetting, User, UserSession
from venvy.schemas import (
    PasswordChangeRequest,
    TotpConfirmRequest,
    TotpDisableRequest,
    UserCreateRequest,
    UserLoginRequest,
)
from venvy.security.dependencies import get_current_user, get_optional_user, resolve_client_ip
from venvy.security.rate_limit import login_limiter

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

SESSION_COOKIE = "session"

#: Prefix for the settings row holding a TOTP secret that has been generated
#: but not yet confirmed. Deleted once enrolment completes.
_TOTP_PENDING_PREFIX = "totp_pending_"


def _issue_session(user: User, response: Response, session: Session) -> str:
    """Create a session row for *user* and set the cookie. Returns the token."""
    token = secrets.token_urlsafe(32)
    session.add(
        UserSession(
            token=token,
            user_id=user.id,
            expires_at=utc_now() + datetime.timedelta(seconds=settings.session_ttl_seconds),
        )
    )
    session.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        path="/",
        max_age=settings.session_ttl_seconds,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return token


def _enforce_password_policy(password: str) -> None:
    """Translate a policy violation into a 400."""
    try:
        validate_password_strength(password)
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/status")
def auth_status(
    user: User | None = Depends(get_optional_user),
    session: Session = Depends(get_session),
):
    """Describe the instance's auth state.

    Unauthenticated by design: the login screen calls this to decide whether to
    show "sign in" or the first-run "create the first admin" flow.
    """
    return {
        "user_exists": session.exec(select(User)).first() is not None,
        "authenticated": user is not None,
        "username": user.username if user else None,
        "role": user.role if user else None,
        "totp_enabled": user.totp_enabled if user else False,
    }


@router.post("/setup", status_code=status.HTTP_201_CREATED)
def auth_setup(
    payload: UserCreateRequest,
    response: Response,
    session: Session = Depends(get_session),
):
    """Create the first admin account.

    Usable exactly once: as soon as any user exists this returns 400, so the
    endpoint cannot be used to add a second admin without authenticating.
    """
    if session.exec(select(User)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Setup has already been completed.")

    _enforce_password_policy(payload.password)
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role="admin",
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    _issue_session(user, response, session)
    audit_log(user.username, "user_setup", "initial admin account created")
    return {"ok": True, "username": user.username, "role": user.role}


@router.post("/login")
def login(
    payload: UserLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Authenticate and start a session.

    When the account has TOTP enabled and no code was supplied, responds 403
    with the detail ``totp_required`` so the client can prompt for the code and
    resubmit — the password is verified before that point, so an attacker
    cannot use this to enumerate which accounts have TOTP without also knowing
    the password.
    """
    rate_key = resolve_client_ip(request)
    login_limiter.check(rate_key)

    user = session.exec(select(User).where(User.username == payload.username)).first()
    if not user or not verify_password(payload.password, user.password_hash):
        lockout = login_limiter.record_failure(rate_key)
        if lockout:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Too many failed attempts. Try again in {lockout} seconds.",
                headers={"Retry-After": str(lockout), "X-Retry-After": str(lockout)},
            )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials.")

    if user.totp_enabled and user.totp_secret:
        if not payload.totp_code:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "totp_required")
        if not totp.verify(user.totp_secret, payload.totp_code):
            login_limiter.record_failure(rate_key)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authenticator code.")

    login_limiter.clear(rate_key)
    _issue_session(user, response, session)
    audit_log(user.username, "login", f"from {rate_key}")
    return {"ok": True, "username": user.username, "role": user.role}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """End the current session.

    Always succeeds, including when there was no session, so a client can call
    it to clear local state without special-casing the response.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = session.get(UserSession, token)
        if row:
            session.delete(row)
            session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.put("/password")
def change_own_password(
    payload: PasswordChangeRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Change the signed-in user's own password."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Current password is incorrect.")
    if payload.password != payload.password_confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "New passwords do not match.")
    _enforce_password_policy(payload.password)

    user.password_hash = hash_password(payload.password)
    session.add(user)
    session.commit()
    audit_log(user.username, "password_change")
    return {"ok": True}


# ── Two-factor authentication ────────────────────────────────────────────────


@router.get("/totp/setup")
def totp_setup_begin(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Generate a pending TOTP secret and its QR provisioning URI.

    Does not enable TOTP — the secret is parked until the user proves their
    authenticator produces matching codes via ``/totp/setup/confirm``. Calling
    this again replaces any pending secret, so an abandoned attempt cannot be
    resumed later with a stale QR code.
    """
    secret = totp.generate_secret()
    key = f"{_TOTP_PENDING_PREFIX}{user.id}"

    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = secret
    else:
        setting = SystemSetting(key=key, value=secret)
    session.add(setting)
    session.commit()

    return {"ok": True, "secret": secret, "uri": totp.provisioning_uri(secret, user.username)}


@router.post("/totp/setup/confirm")
def totp_setup_confirm(
    payload: TotpConfirmRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verify a code against the pending secret and enable TOTP."""
    key = f"{_TOTP_PENDING_PREFIX}{user.id}"
    setting = session.get(SystemSetting, key)
    if not setting or not setting.value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No pending TOTP setup — start setup first."
        )
    if not totp.verify(setting.value, payload.totp_code):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Incorrect code — try again.")

    user.totp_secret = setting.value
    user.totp_enabled = True
    session.add(user)
    session.delete(setting)
    session.commit()
    audit_log(user.username, "totp_enable")
    return {"ok": True}


@router.post("/totp/disable")
def totp_disable(
    payload: TotpDisableRequest,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Turn TOTP off, after re-confirming the account password."""
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password.")

    user.totp_enabled = False
    user.totp_secret = None
    session.add(user)
    session.commit()
    audit_log(user.username, "totp_disable")
    return {"ok": True}
