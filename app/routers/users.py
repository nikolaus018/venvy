import asyncio
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.auth import _validate_password_strength, check_admin, get_current_user, hash_password
from app.database import App, CfRoute, User, UserSession, get_session
from app.schemas import UserReq
from app.utils.process import _cancel_log_task, _kill_pid_tree

router = APIRouter()

# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------


@router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    _: User = Depends(check_admin),
):
    return [
        {"id": u.id, "username": u.username, "is_admin": u.is_admin}
        for u in session.exec(select(User)).all()
    ]


@router.post("/users")
def create_user(
    req: UserReq,
    session: Session = Depends(get_session),
    _: User = Depends(check_admin),
):
    if session.exec(select(User).where(User.username == req.username)).first():
        raise HTTPException(400, "Username taken.")
    _validate_password_strength(req.password)
    session.add(
        User(
            username=req.username,
            password_hash=hash_password(req.password),
            is_admin=False,
        )
    )
    session.commit()
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
        for app_row in session.exec(select(App).where(App.user_id == user_id)).all():
            if app_row.pid:
                try:
                    await _kill_pid_tree(app_row.pid)
                except Exception:
                    pass
            _cancel_log_task(app_row.id)
            cf_route = session.exec(
                select(CfRoute).where(CfRoute.app_id == app_row.id)
            ).first()
            if cf_route:
                session.delete(cf_route)
            if await asyncio.to_thread(Path(app_row.app_dir).exists):
                await asyncio.to_thread(
                    shutil.rmtree, app_row.app_dir, ignore_errors=True
                )
            session.delete(app_row)

        # Delete all sessions for the user to prevent orphaned session rows
        # and ensure the deleted account cannot be re-authenticated.
        for user_session in session.exec(
            select(UserSession).where(UserSession.user_id == user_id)
        ).all():
            session.delete(user_session)

        session.delete(user)
        session.commit()
    return {"ok": True}
