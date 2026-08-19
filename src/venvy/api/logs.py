"""Reading and clearing an app's log file."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session

from venvy.core.audit import audit_log
from venvy.db.engine import get_session
from venvy.db.models import App, User
from venvy.security.dependencies import get_current_user, require_operator
from venvy.security.rate_limit import rate_limit_mutations

__all__ = ["router"]

router = APIRouter(prefix="/apps/{app_id}/logs", tags=["logs"])

#: Never read more than this from the end of a log file. A long-running app can
#: produce hundreds of megabytes, and the UI only ever shows the tail.
_MAX_TAIL_BYTES = 512 * 1024


def _get_app_or_404(session: Session, app_id: int) -> App:
    """Fetch an app or raise 404."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


def _log_path(app: App) -> Path:
    """Return the app's log file path."""
    return Path(app.app_dir) / "logs" / "app.log"


def _read_tail(log_file: Path, max_lines: int) -> dict:
    """Read up to *max_lines* lines from the end of *log_file*."""
    try:
        size = log_file.stat().st_size
        with open(log_file, "rb") as handle:
            if size > _MAX_TAIL_BYTES:
                handle.seek(-_MAX_TAIL_BYTES, 2)
                chunk = handle.read().decode("utf-8", errors="replace")
                # The seek almost certainly landed mid-line; drop the fragment
                # so the first line shown is a real one.
                chunk = chunk[chunk.find("\n") + 1 :]
            else:
                chunk = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return {"content": "", "truncated": False, "total_lines": 0}

    lines = chunk.splitlines()
    if len(lines) > max_lines:
        return {
            "content": "\n".join(lines[-max_lines:]),
            "truncated": True,
            "total_lines": len(lines),
        }
    return {"content": chunk, "truncated": False, "total_lines": len(lines)}


@router.get("")
async def get_app_logs(
    app_id: int,
    lines: int = Query(default=2000, ge=1, le=10000),
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Return the tail of the app's log."""
    app = _get_app_or_404(session, app_id)
    log_file = _log_path(app)
    if not log_file.exists():
        return {"content": "", "truncated": False, "total_lines": 0}
    return await asyncio.to_thread(_read_tail, log_file, lines)


@router.delete("")
async def clear_app_logs(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Truncate the app's log file.

    Truncates rather than deletes so the running process keeps writing to the
    same open descriptor; deleting the file would leave it writing to an
    unlinked inode and the UI would show nothing more until a restart.
    """
    app = _get_app_or_404(session, app_id)
    log_file = _log_path(app)
    if log_file.exists():
        await asyncio.to_thread(log_file.write_text, "", encoding="utf-8")
    audit_log(user.username, "logs_clear", f"'{app.name}' (id={app_id})")
    return {"ok": True}
