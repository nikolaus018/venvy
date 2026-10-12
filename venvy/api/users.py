"""User management routes (admin only)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import select, Session

from venvy.api.auth import UserReq
from venvy.audit import audit_log
from venvy.auth import (
    check_admin,
    check_ip_allowlist,
    _clean_username,
    hash_password,
    _validate_password_strength,
)
from venvy.db import get_session
from venvy.models import SystemSetting, User, UserSession, VALID_ROLES

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist)]
)


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    _: User = Depends(check_admin),
):
    return [
        {"id": u.id, "username": u.username, "role": u.role}
        for u in session.exec(select(User)).all()
    ]


@router.post("/users")
def create_user(
    req: UserReq,
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
):
    username = _clean_username(req.username)
    if session.exec(select(User).where(User.username == username)).first():
        raise HTTPException(400, "Username taken.")
    _validate_password_strength(req.password)
    role = req.role or "operator"
    if role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(VALID_ROLES)}")
    session.add(User(
        username=username,
        password_hash=hash_password(req.password),
        role=role,
    ))
    try:
        session.commit()
    except IntegrityError:
        # Lost a race with another request creating the same name.
        session.rollback()
        raise HTTPException(400, "Username taken.")
    audit_log(admin.username, "user_create", f"created user '{username}' with role '{role}'")
    return {"ok": True}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(check_admin),
):
    if user_id == current_user.id:
        raise HTTPException(400, "Cannot delete yourself.")
    user = session.get(User, user_id)
    if user:
        # Apps belong to the instance, not to individual users — deleting a user
        # must never delete or stop any app. Only the account and its sessions go.
        for db_session_row in session.exec(select(UserSession).where(UserSession.user_id == user_id)).all():
            session.delete(db_session_row)
        # An authenticator enrolment that was started and never confirmed.
        # User ids are reused, so left behind it would be offered to
        # whoever gets this id next.
        pending_totp = session.get(SystemSetting, f"totp_pending_{user_id}")
        if pending_totp:
            session.delete(pending_totp)
        session.delete(user)
        session.commit()
        audit_log(current_user.username, "user_delete", f"deleted user '{user.username}' (id={user_id})")
    return {"ok": True}
