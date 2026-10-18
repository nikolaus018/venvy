"""User management routes (admin only)."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
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
from venvy.models import ApiToken, App, AppAccess, SystemSetting, User, UserSession, VALID_ROLES

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
    access: dict = {}
    for row in session.exec(select(AppAccess)).all():
        access.setdefault(row.user_id, []).append(row.app_id)
    return [
        {"id": u.id, "username": u.username, "role": u.role, "all_apps": u.all_apps or u.role == "admin",
         "app_ids": sorted(access.get(u.id, []))}
        for u in session.exec(select(User)).all()
    ]


class AccessReq(BaseModel):
    all_apps: bool = True
    app_ids: List[int] = []


@router.put("/users/{user_id}/access")
def set_user_access(
    user_id: int,
    req: AccessReq,
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
):
    """Which apps a user may see and use: all of them, or just the listed ones. Admins always see all."""
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404)
    if user.role == "admin" and not req.all_apps:
        raise HTTPException(400, "An admin always has access to every app.")
    known = {a.id for a in session.exec(select(App)).all()}
    unknown = [i for i in req.app_ids if i not in known]
    if unknown:
        raise HTTPException(400, f"No such app: {unknown[0]}")
    user.all_apps = req.all_apps
    session.add(user)
    for row in session.exec(select(AppAccess).where(AppAccess.user_id == user_id)).all():
        session.delete(row)
    session.flush()
    if not req.all_apps:
        for app_id in sorted(set(req.app_ids)):
            session.add(AppAccess(user_id=user_id, app_id=app_id))
    session.commit()
    audit_log(admin.username, "user_access_change",
              f"'{user.username}': " + ("all apps" if req.all_apps else f"apps {sorted(set(req.app_ids))}"))
    return {"ok": True}


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
        for row in session.exec(select(AppAccess).where(AppAccess.user_id == user_id)).all():
            session.delete(row)
        for tok in session.exec(select(ApiToken).where(ApiToken.user_id == user_id)).all():
            session.delete(tok)
        session.delete(user)
        session.commit()
        audit_log(current_user.username, "user_delete", f"deleted user '{user.username}' (id={user_id})")
    return {"ok": True}
