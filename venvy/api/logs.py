"""Searching and downloading an app's logs, including the rotated ones."""
import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Iterator, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session

from venvy.audit import audit_log
from venvy.auth import check_ip_allowlist, get_current_user, get_download_user, require_app_access
from venvy.config import ASSETS_DIR
from venvy.db import get_session
from venvy.isolation import _app_log_file
from venvy.models import App, User

router = APIRouter(
    prefix="/api", tags=["logs"], dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)]
)

_SCRIPT = ASSETS_DIR / "venvy" / "runtime_assets" / "search_logs.py"
_ROTATED = re.compile(r"\.(\d+)\Z")


def log_files(app: App) -> List[Path]:
    """The app's log files, oldest first (rotated ones, then the current one)."""
    current = _app_log_file(app)
    found = []
    with contextlib.suppress(OSError):
        for p in current.parent.iterdir():
            m = _ROTATED.search(p.name)
            if p.name.startswith(current.name + ".") and m and p.is_file():
                found.append((int(m.group(1)), p))
    files = [p for _, p in sorted(found)]
    if current.is_file():
        files.append(current)
    return files


@router.get("/apps/{app_id}/logs/search")
async def search_logs(
    app_id: int,
    q: str = Query(min_length=1, max_length=300),
    regex: bool = False,
    case: bool = False,
    limit: int = Query(default=200, ge=1, le=2000),
    context: int = Query(default=0, ge=0, le=5),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Find lines in all of the app's logs (rotated ones included), oldest first."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(404)
    files = await asyncio.to_thread(log_files, app)
    if not files:
        return {"matches": [], "truncated": False, "scanned_bytes": 0, "files": 0}
    payload = json.dumps({"files": [str(f) for f in files], "query": q, "regex": regex, "case": case,
                          "limit": limit, "context": context}).encode()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(_SCRIPT),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(payload), timeout=20)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise HTTPException(408, "The search took too long — try a simpler pattern or plain text.")
    try:
        result = json.loads(out or b"{}")
    except ValueError:
        raise HTTPException(500, "The search failed.")
    if "error" in result:
        raise HTTPException(400, result["error"])
    result["files"] = len(files)
    return result


def _stream(files: List[Path]) -> Iterator[bytes]:
    for f in files:
        try:
            with open(f, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    yield chunk
        except OSError:
            continue


@router.get("/apps/{app_id}/logs/download")
def download_logs(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_download_user),
):
    """All of the app's logs as one text file."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(404)
    files = log_files(app)
    audit_log(user.username, "app_logs_download", f"app '{app.name}' (id={app_id})")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", app.slug) or "app"
    return StreamingResponse(_stream(files), media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{safe}-logs.txt"'})
