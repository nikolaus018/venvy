"""User administration. Every route here requires the admin role."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from venvy.core.audit import audit_log
from venvy.core.passwords import PasswordPolicyError, hash_password, validate_password_strength
from venvy.db.engine import get_session
from venvy.db.models import User, UserSession
from venvy.schemas import UserCreateRequest
from venvy.security.dependencies import check_admin
from venvy.security.rate_limit import rate_limit_mutations

__all__ = ["router"]

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
def list_users(
    session: Session = Depends(get_session),
    _admin: User = Depends(check_admin),
):
    """List every account. Password hashes and TOTP secrets are never included."""
    return [
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "totp_enabled": user.totp_enabled,
        }
        for user in session.exec(select(User).order_by(User.username)).all()
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreateRequest,
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Create an account. Defaults to the ``operator`` role."""
    if session.exec(select(User).where(User.username == payload.username)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That username is taken.")

    try:
        validate_password_strength(payload.password)
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    role = payload.role or "operator"
    session.add(
        User(
            username=payload.username,
            password_hash=hash_password(payload.password),
            role=role,
        )
    )
    session.commit()
    audit_log(admin.username, "user_create", f"created '{payload.username}' with role '{role}'")
    return {"ok": True}


@router.delete("/{user_id}")
def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Delete an account and revoke its sessions.

    Apps belong to the instance rather than to their creator, so this never
    deletes or stops an app — only the account and its sessions go. Deleting
    yourself is refused, which keeps at least one admin in place.
    """
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account.")

    user = session.get(User, user_id)
    if not user:
        # Idempotent: the caller's goal — that this account not exist — holds.
        return {"ok": True}

    for row in session.exec(select(UserSession).where(UserSession.user_id == user_id)).all():
        session.delete(row)
    session.delete(user)
    session.commit()
    audit_log(admin.username, "user_delete", f"deleted '{user.username}' (id={user_id})")
    return {"ok": True}
