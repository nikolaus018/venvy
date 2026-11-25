"""File manager routes: tree, file operations, download, uploads."""
import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlmodel import Session

from venvy.audit import audit_log
from venvy.auth import check_ip_allowlist, get_download_user, rate_limit_mutations, require_app_access, require_operator
from venvy.config import _UPLOAD_TMP_DIR
from venvy.db import get_session
from venvy.isolation import _app_ids
from venvy.models import App, User
from venvy.safefs import (
    _app_rel_parts,
    AppPathError,
    _file_download_response,
    _fs_delete,
    _fs_install,
    _fs_listdir,
    _fs_mkdir,
    _fs_move_or_copy,
    _fs_open_file,
    _fs_save,
    _fs_touch,
    _fs_write_stream,
    _is_protected_app_file,
    is_safe_path,
    _upload_rel_path_error,
)
from venvy.uploads import (
    _append_upload_chunk_locked,
    _part_lock,
    _purge_stale_upload_parts,
    _release_upload_reserve,
    _SINGLE_REQUEST_UPLOAD_MAX,
    _UPLOAD_FILE_MAX,
    _upload_part_path,
)

_TREE_HASH_MAX_BYTES = 512 * 1024 * 1024
_MAX_ACTIVE_UPLOADS_PER_USER = 32

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)]
)


class FileOpReq(BaseModel):
    operation: str
    path: str
    new_path: Optional[str] = None
    content: Optional[str] = None


@router.get("/apps/{app_id}/files/tree")
def file_tree(
    app_id: int,
    path: str = "",
    hashes: bool = False,
    session: Session = Depends(get_session),
    # A viewer's role is explicitly "read-only: status, logs, metrics" — not
    # file contents. App directories routinely contain .env files, config
    # with DB credentials, service-account JSON, etc., so this needs the same
    # gate as file_operations() (mutations) and env_json redaction, not the
    # bare-authenticated get_current_user any role can satisfy.
    user: User = Depends(require_operator),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    try:
        parts = _app_rel_parts(root, path)
    except AppPathError as exc:
        raise HTTPException(exc.status)
    results = []
    hashed_total = 0
    _SKIP_NAMES = {"venv", "__pycache__", "logs", "node_modules", ".git", ".mypy_cache", ".pytest_cache"}
    for item in _fs_listdir(root, parts):
        if item["name"] in _SKIP_NAMES or _is_protected_app_file(item["name"]):
            continue
        entry = {
            "name": item["name"],
            "path": "/".join(parts + (item["name"],)),
            "is_dir": item["is_dir"],
            "size": item["size"],
        }
        if hashes and not item["is_dir"]:
            remaining = _TREE_HASH_MAX_BYTES - hashed_total
            digest, read_bytes = _file_sha256(root, parts + (item["name"],), remaining)
            entry["sha256"] = digest
            hashed_total += read_bytes
        results.append(entry)
    return sorted(results, key=lambda x: (not x["is_dir"], x["name"].lower()))


_HASH_MAX_BYTES = 256 * 1024 * 1024


def _file_sha256(root: Path, parts: tuple, max_budget: int = _HASH_MAX_BYTES):
    """SHA-256 of a regular file in the app (None for a link, something else, or one
    over the per-file / per-request byte budget) — returns (hex_or_None, bytes_read)."""
    cap = min(_HASH_MAX_BYTES, max(0, max_budget))
    if cap <= 0:
        return None, 0
    try:
        f, st = _fs_open_file(root, parts)
    except (AppPathError, OSError):
        return None, 0
    with f:
        if st.st_size > cap:
            return None, 0
        h = hashlib.sha256()
        total = 0
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                return None, total
            h.update(chunk)
        return h.hexdigest(), total


@router.post("/apps/{app_id}/files/op")
async def file_operations(
    app_id: int,
    req: FileOpReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    op = req.operation
    if op not in ("create_file", "create_folder", "delete", "rename", "copy", "save"):
        raise HTTPException(400, f"Unknown operation: {op}")
    if "\x00" in req.path or (req.new_path is not None and "\x00" in req.new_path):
        raise HTTPException(400, "Invalid path.")

    try:
        # delete/rename/copy act on the entry itself; the others on what it names.
        parts = _app_rel_parts(root, req.path, follow_last=op not in ("delete", "rename", "copy"))
        if parts and _is_protected_app_file(parts[-1]):
            raise HTTPException(403, "Operation not permitted on this path.")
        if not parts and op != "create_folder":
            raise HTTPException(400, "That operation isn't allowed on the app's root folder.")
        owner = _app_ids(app_row)   # new files and folders go to the app's user

        if op == "create_file":
            await asyncio.to_thread(_fs_touch, root, parts, owner)

        elif op == "create_folder":
            await asyncio.to_thread(_fs_mkdir, root, parts, owner)

        elif op == "delete":
            await asyncio.to_thread(_fs_delete, root, parts)

        elif op in ("rename", "copy"):
            new_parts = _app_rel_parts(root, req.new_path or "", follow_last=False)
            if not new_parts:
                raise HTTPException(403)
            if _is_protected_app_file(new_parts[-1]):
                raise HTTPException(403, "Operation not permitted on this path.")
            await asyncio.to_thread(
                _fs_move_or_copy, root, parts, new_parts, op == "copy", owner, req.new_path or "")

        elif op == "save":
            if req.content is None:
                raise HTTPException(400, "No content provided for save.")
            await asyncio.to_thread(_fs_save, root, parts, req.content.encode("utf-8"), owner)
    except HTTPException:
        raise
    except AppPathError as exc:
        raise HTTPException(exc.status, exc.detail or None)
    except Exception as exc:
        reason = str(exc).replace("\r", " ").replace("\n", " ")
        logging.error(f"File operation '{op}' failed for app {app_id}: {reason}")
        raise HTTPException(500, "File operation failed.")
    audit_log(user.username, f"file_{op}", f"app '{app_row.name}' (id={app_id}): {req.path}")
    return {"ok": True}


@router.get("/apps/{app_id}/files/download")
async def download_file(
    app_id: int,
    path: str,
    session: Session = Depends(get_session),
    # See file_tree() above — same reasoning, viewers don't get file contents.
    user: User = Depends(get_download_user),
):
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator or admin privileges required.")
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    try:
        parts = _app_rel_parts(root, path)
        if not parts:
            raise HTTPException(400, "Invalid file path.")
        if _is_protected_app_file(parts[-1]):
            raise HTTPException(403, "Operation not permitted on this path.")
        fileobj, st = await asyncio.to_thread(_fs_open_file, root, parts)
    except AppPathError as exc:
        raise HTTPException(exc.status, exc.detail or None)
    audit_log(user.username, "file_download", f"app '{app_row.name}' (id={app_id}): {'/'.join(parts)}")
    return _file_download_response(fileobj, st, parts[-1])


@router.post("/apps/{app_id}/files/upload")
async def upload_file_to_path(
    app_id: int,
    path: str = Form(""),
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if "\x00" in path:
        raise HTTPException(400, "Invalid path.")
    root = Path(app_row.app_dir)
    try:
        target_dir = await asyncio.to_thread(lambda: (root / path).resolve())
    except (OSError, ValueError):
        raise HTTPException(400, "Invalid path.")
    if not is_safe_path(root, target_dir):
        raise HTTPException(403)
    if not await asyncio.to_thread(target_dir.is_dir):
        raise HTTPException(404, "Target directory does not exist.")
    owner = _app_ids(app_row)
    skipped: List[Dict[str, str]] = []
    uploaded = 0
    for file in files:
        rel_path = file.filename or "upload"
        reason = _upload_rel_path_error(rel_path)
        parts: tuple = ()
        if not reason:
            try:
                parts = _app_rel_parts(root, str(target_dir / rel_path))
                if parts and _is_protected_app_file(parts[-1]):
                    reason = "protected file"
            except AppPathError:
                reason = "path escapes app directory"
        if not reason:
            file.file.seek(0)
            try:
                if not await asyncio.to_thread(_fs_write_stream, root, parts, file.file, _SINGLE_REQUEST_UPLOAD_MAX, owner):
                    reason = f"exceeds the {_SINGLE_REQUEST_UPLOAD_MAX // (1024 * 1024)} MB single-request limit"
                else:
                    uploaded += 1
            except AppPathError as exc:
                reason = exc.detail or "invalid path"
        if reason:
            skipped.append({"path": rel_path, "reason": reason})
    if uploaded:
        audit_log(user.username, "file_upload", f"app '{app_row.name}' (id={app_id}): {uploaded} file(s)")
    return {"ok": True, "skipped": skipped}


@router.post("/apps/{app_id}/files/upload/chunk")
async def upload_file_chunk(
    app_id: int,
    upload_id: str = Form(...),
    path: str = Form(""),
    rel_path: str = Form(...),
    offset: int = Form(...),
    total: int = Form(...),
    chunk: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    # Deliberately not rate_limit_mutations: one large file is hundreds of
    # chunk requests. Access still requires an operator session + CSRF.
):
    """Append one chunk of a file upload. The client sends a file's chunks in
    order; offset must equal the bytes received so far (a mismatch answers
    409 with the server's count, so the client can resume from there). When
    the part reaches *total* bytes it is moved into place atomically."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if "\x00" in path or "\x00" in rel_path:
        raise HTTPException(400, "Invalid path.")
    root = Path(app_row.app_dir)
    try:
        target_dir = await asyncio.to_thread(lambda: (root / path).resolve())
    except (OSError, ValueError):
        raise HTTPException(400, "Invalid path.")
    if not is_safe_path(root, target_dir):
        raise HTTPException(403)
    if not await asyncio.to_thread(target_dir.is_dir):
        raise HTTPException(404, "Target directory does not exist.")
    reason = _upload_rel_path_error(rel_path)
    if reason:
        raise HTTPException(400, f"'{rel_path}': {reason}.")
    try:
        dest_parts = _app_rel_parts(root, str(target_dir / rel_path))
    except AppPathError:
        raise HTTPException(403, "Upload path escapes app directory.")
    if dest_parts and _is_protected_app_file(dest_parts[-1]):
        raise HTTPException(403, "Operation not permitted on this path.")
    if total < 0 or offset < 0 or offset > total:
        raise HTTPException(400, "Invalid offset/total.")
    if total > _UPLOAD_FILE_MAX:
        raise HTTPException(413, f"File exceeds the {_UPLOAD_FILE_MAX // (1024 * 1024)} MB upload limit.")

    part = _upload_part_path(app_id, user.id, upload_id)
    owner = _app_ids(app_row)

    def _append_and_maybe_finalize():
        with _part_lock(part):
            _UPLOAD_TMP_DIR.mkdir(mode=0o700, exist_ok=True)
            if offset == 0:
                _purge_stale_upload_parts()
                active = sum(1 for p in _UPLOAD_TMP_DIR.glob(f"*-{user.id}-*.part") if p != part)
                if active >= _MAX_ACTIVE_UPLOADS_PER_USER:
                    raise HTTPException(429, "Too many uploads in progress — wait for one to finish or cancel it.")
            received = _append_upload_chunk_locked(part, chunk, offset, total)
            if received < total:
                return received, False
            try:
                _fs_install(root, dest_parts, part, owner)
            except AppPathError as exc:
                part.unlink(missing_ok=True)
                raise HTTPException(exc.status, exc.detail or f"'{rel_path}' cannot be written.")
            finally:
                _release_upload_reserve(part)
            return received, True

    received, done = await asyncio.to_thread(_append_and_maybe_finalize)
    if done:
        audit_log(user.username, "file_upload", f"app '{app_row.name}' (id={app_id}): {'/'.join(dest_parts)}")
    return {"ok": True, "received": received, "done": done}


@router.delete("/apps/{app_id}/files/upload/{upload_id}")
async def abort_file_upload(
    app_id: int,
    upload_id: str,
    user: User = Depends(require_operator),
):
    """Discard a partially uploaded file (e.g. after the user cancels)."""
    part = _upload_part_path(app_id, user.id, upload_id)

    def _abort() -> None:
        with _part_lock(part):
            part.unlink(missing_ok=True)
            _release_upload_reserve(part)

    await asyncio.to_thread(_abort)
    return {"ok": True}
