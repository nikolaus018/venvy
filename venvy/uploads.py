"""Upload limits and the temp-file handling behind chunked uploads.

The UI sends every file through the chunked endpoints, so neither a proxy's
request-body cap (Cloudflare: 100 MB) nor the multipart parser's per-request
file-count limit applies. The single-request endpoints remain for API clients.
"""
import contextlib
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Dict

from fastapi import HTTPException, UploadFile

from venvy.config import _UPLOAD_TMP_DIR

# A fixed set: one lock per upload would need evicting, and an evicted lock
# that a thread is about to take no longer excludes anyone.
_upload_part_locks = tuple(threading.Lock() for _ in range(256))


def _part_lock(part: Path) -> threading.Lock:
    return _upload_part_locks[hash(str(part)) % len(_upload_part_locks)]


_SINGLE_REQUEST_UPLOAD_MAX = 50 * 1024 * 1024

_UPLOAD_CHUNK_MAX = 16 * 1024 * 1024

_UPLOAD_FILE_MAX = int(os.getenv("VENVY_MAX_UPLOAD_MB", "10240")) * 1024 * 1024

_UPLOAD_STALE_SEC = 24 * 3600

_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")

_UPLOAD_COPY_BUF = 1024 * 1024

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)

# Track reserved remaining bytes across concurrent uploads so parallel
# uploads cannot collectively bypass the 256 MB free-space floor.
_reserve_lock = threading.Lock()
_reserved_bytes: Dict[str, int] = {}


def _release_upload_reserve(part: Path) -> None:
    with _reserve_lock:
        _reserved_bytes.pop(str(part), None)


def _save_upload_file(file: UploadFile, dest: Path, limit: int) -> bool:
    """Stream *file* to *dest* without holding it in memory. Returns False (and
    removes the partial file) if it is larger than *limit* bytes."""
    written = 0
    file.file.seek(0)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW | _O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                buf = file.file.read(_UPLOAD_COPY_BUF)
                if not buf:
                    break
                written += len(buf)
                if written > limit:
                    out.close()
                    dest.unlink(missing_ok=True)
                    return False
                out.write(buf)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return True


def _append_upload_chunk_locked(part: Path, chunk: UploadFile, offset: int, total: int) -> int:
    """Append one chunk to *part*. Caller holds `_part_lock(part)`."""
    if total < 0 or offset < 0 or offset > total:
        raise HTTPException(400, "Invalid offset/total.")
    if total > _UPLOAD_FILE_MAX:
        raise HTTPException(413, f"File exceeds the {_UPLOAD_FILE_MAX // (1024 * 1024)} MB upload limit.")
    if offset == 0:
        have = 0
        mode = "wb"
    else:
        try:
            have = part.stat().st_size
        except FileNotFoundError:
            have = 0
        if have != offset:
            raise HTTPException(409, {"message": "Offset mismatch.", "received": have})
        mode = "ab"
    remaining = max(0, total - have)
    key = str(part)
    with _reserve_lock:
        other_reserved = sum(v for k, v in _reserved_bytes.items() if k != key)
        free = shutil.disk_usage(part.parent).free
        if remaining + other_reserved + 256 * 1024 * 1024 > free:
            raise HTTPException(507, "Not enough free disk space for this upload.")
        _reserved_bytes[key] = remaining
    chunk.file.seek(0)
    written = 0
    try:
        with open(part, mode) as out:
            while True:
                buf = chunk.file.read(_UPLOAD_COPY_BUF)
                if not buf:
                    break
                written += len(buf)
                if written > _UPLOAD_CHUNK_MAX or offset + written > total:
                    out.flush()
                    if offset == 0:
                        out.close()
                        part.unlink(missing_ok=True)
                        _release_upload_reserve(part)
                    else:
                        # Roll back only this bad chunk instead of discarding a multi-GB upload.
                        out.truncate(offset)
                    raise HTTPException(400, "Chunk is larger than allowed.")
                out.write(buf)
    except HTTPException:
        raise
    except BaseException:
        with contextlib.suppress(OSError):
            if offset == 0:
                part.unlink(missing_ok=True)
                _release_upload_reserve(part)
            elif part.exists():
                os.truncate(part, offset)
        raise
    new_pos = offset + written
    with _reserve_lock:
        if new_pos >= total:
            _reserved_bytes.pop(key, None)
        else:
            _reserved_bytes[key] = max(0, total - new_pos)
    return new_pos


def _append_upload_chunk(part: Path, chunk: UploadFile, offset: int, total: int) -> int:
    """Append one chunk to *part* and return the bytes it now holds. The
    client sends chunks in order: *offset* must equal what has arrived so far
    (a mismatch answers 409 with the real count, so the client resumes from
    there). Checks there is room for the remaining bytes. Blocking."""
    with _part_lock(part):
        return _append_upload_chunk_locked(part, chunk, offset, total)


def _purge_stale_parts(pattern_dir: Path, pattern: str) -> None:
    """Delete .part files untouched for a day, and forget their disk reservation.
    The callers hold one part's lock already (and the locks are striped, not
    reentrant): a part whose lock is taken is skipped, not waited for — it is
    in use, or it is the next sweep's."""
    cutoff = time.time() - _UPLOAD_STALE_SEC
    try:
        for part in pattern_dir.glob(pattern):
            lock = _part_lock(part)
            if not lock.acquire(blocking=False):
                continue
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    _release_upload_reserve(part)
            except OSError:
                pass
            finally:
                lock.release()
    except OSError:
        pass


def _purge_stale_upload_parts() -> None:
    _purge_stale_parts(_UPLOAD_TMP_DIR, "*.part")


def _upload_part_path(app_id: int, user_id: int, upload_id: str) -> Path:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(400, "Invalid upload_id.")
    return _UPLOAD_TMP_DIR / f"{app_id}-{user_id}-{upload_id}.part"
