"""Updating Venvy from the UI (admin only). See venvy/updates.py."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from venvy import updates
from venvy.audit import audit_log
from venvy.auth import check_admin, check_ip_allowlist, get_session_user, rate_limit_mutations
from venvy.models import User

router = APIRouter(
    prefix="/api", tags=["system"], dependencies=[Depends(check_ip_allowlist)]
)


@router.get("/system/update")
async def update_info(refresh: bool = False, _: User = Depends(check_admin)):
    """The running version, the newest release, and how the last update went.
    The newest release is looked up at most once an hour unless refresh=true."""
    current = updates.current_version()
    latest, error = await updates.latest_version(force=refresh)
    return {
        "current": current,
        "latest": latest,
        "update_available": updates.is_newer(latest, current),
        "check_error": error,
        "checked_at": updates.last_checked(),
        "supported": updates.unsupported_reason() is None,
        "unsupported_reason": updates.unsupported_reason(),
        **await asyncio.to_thread(updates.state),   # may ask systemctl: off the event loop
    }


@router.get("/system/update/notes")
async def update_notes(refresh: bool = False, _: User = Depends(check_admin)):
    """What changed: the release notes of every version newer than the running one, newest
    first (newer: true) — or, when it is up to date, the latest release's own (newer: false)."""
    current = updates.current_version()
    latest, _ = await updates.latest_version(force=refresh)
    sections, newer, error = await updates.release_notes(current, latest, force=refresh)
    return {"current": current, "latest": latest, "newer": newer, "releases": sections, "error": error}


@router.post("/system/update")
def start_update(admin: User = Depends(check_admin), _session: User = Depends(get_session_user),
                 _rl: None = Depends(rate_limit_mutations)):
    """Update to the newest release. Answers at once; the update stops and restarts Venvy,
    so follow it with GET /api/health and GET /api/system/update."""
    reason = updates.unsupported_reason()
    if reason:
        raise HTTPException(400, reason)
    if updates.busy():
        raise HTTPException(409, "An update is already under way.")
    try:
        updates.request()
    except FileExistsError:
        raise HTTPException(409, "An update is already under way.")
    except OSError as exc:
        raise HTTPException(500, f"Could not ask for the update: {exc}")
    audit_log(admin.username, "system_update", f"from {updates.current_version()}")
    return {"ok": True}
