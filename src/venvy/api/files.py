"""The file browser: listing, reading, writing and uploading inside an app.

Every path in this module is user-supplied. All of them go through
:func:`_resolve` before anything touches the filesystem, which is the single
place containment is enforced.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlmodel import Session

from venvy.config import settings
from venvy.core.paths import (
    SKIPPED_TREE_NAMES,
    is_protected_name,
    is_safe_path,
    is_safe_upload_name,
)
from venvy.db.engine import get_session
from venvy.db.models import App, User
from venvy.schemas import FileOperationRequest
from venvy.security.dependencies import get_current_user, require_operator
from venvy.security.rate_limit import rate_limit_mutations

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps/{app_id}/files", tags=["files"])


def _get_app_or_404(session: Session, app_id: int) -> App:
    """Fetch an app or raise 404."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


def _resolve(root: Path, relative: str) -> Path:
    """Resolve *relative* inside *root*, refusing anything that escapes.

    Also refuses the jail-support files, which back the terminal's directory
    restriction: letting them be rewritten or deleted would defeat it.
    """
    target = (root / relative).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path is outside the app directory.")
    if is_protected_name(target.name):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "That file is managed by Venvy and cannot be changed."
        )
    return target


@router.get("/tree")
def file_tree(
    app_id: int,
    path: str = "",
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """List one directory level, directories first then files, alphabetically.

    Hidden files and build/vendor directories are omitted — they are noise in a
    browser meant for editing source.
    """
    app = _get_app_or_404(session, app_id)
    root = Path(app.app_dir)
    target = _resolve(root, path)

    if not target.is_dir():
        return []

    entries = []
    for item in target.iterdir():
        if item.name.startswith(".") or item.name in SKIPPED_TREE_NAMES:
            continue
        try:
            size = item.stat().st_size
        except OSError:
            size = 0
        entries.append(
            {
                "name": item.name,
                "path": item.relative_to(root).as_posix(),
                "is_dir": item.is_dir(),
                "size": size,
            }
        )
    return sorted(entries, key=lambda entry: (not entry["is_dir"], entry["name"].lower()))


@router.get("/download")
def download_file(
    app_id: int,
    path: str,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Download a single file."""
    app = _get_app_or_404(session, app_id)
    target = _resolve(Path(app.app_dir), path)

    if not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")

    return FileResponse(
        target,
        filename=target.name,
        headers={
            # App source changes constantly; a cached copy in the editor would
            # silently show stale contents.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


async def _perform_operation(request: FileOperationRequest, root: Path, target: Path) -> None:
    """Execute one validated filesystem operation."""
    operation = request.operation

    if operation == "create_file":
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.touch)

    elif operation == "create_folder":
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)

    elif operation == "delete":
        if await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.rmtree, target, ignore_errors=True)
        else:
            await asyncio.to_thread(target.unlink, missing_ok=True)

    elif operation in ("rename", "copy"):
        destination = _resolve(root, request.new_path or "")
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        if operation == "rename":
            await asyncio.to_thread(target.rename, destination)
        elif await asyncio.to_thread(target.is_dir):
            await asyncio.to_thread(shutil.copytree, target, destination)
        else:
            await asyncio.to_thread(shutil.copy2, target, destination)

    elif operation == "save":
        if request.content is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No content provided for save.")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        # newline="" disables the platform's newline translation, so the file on
        # disk holds exactly the bytes the editor sent. Without it, saving on a
        # Windows host would silently rewrite every LF as CRLF.
        async with aiofiles.open(target, "w", encoding="utf-8", newline="") as handle:
            await handle.write(request.content)


@router.post("/op")
async def file_operations(
    app_id: int,
    request: FileOperationRequest,
    session: Session = Depends(get_session),
    _user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Create, delete, rename, copy or save a file or folder."""
    app = _get_app_or_404(session, app_id)
    root = Path(app.app_dir)
    target = _resolve(root, request.path)

    try:
        await _perform_operation(request, root, target)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"File operation failed: {exc}"
        ) from exc
    return {"ok": True}


@router.post("/upload")
async def upload_file_to_path(
    app_id: int,
    path: str = Form(""),
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    _user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Upload files into an existing directory within the app.

    Unlike app creation, an unsafe filename here is an error rather than a
    silent skip: the user picked these files deliberately and needs to know
    if one was rejected.
    """
    app = _get_app_or_404(session, app_id)
    root = Path(app.app_dir)
    target_dir = _resolve(root, path)

    if not target_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Target directory does not exist.")

    for upload in files:
        relative = upload.filename or "upload"
        if "\0" in relative or ".." in relative or relative.startswith("/"):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Rejected unsafe upload path: {relative!r}"
            )

        name = Path(relative).name
        if not is_safe_upload_name(name):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"Filename {name!r} contains invalid characters."
            )

        destination = (target_dir / relative).resolve()
        if not is_safe_path(root, destination):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Upload path escapes the app directory.")

        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        data = await upload.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"File {name!r} exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit.",
            )
        async with aiofiles.open(destination, "wb") as handle:
            await handle.write(data)

    return {"ok": True}
