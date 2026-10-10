"""Upload limits and the temp-file handling behind chunked uploads.

The UI sends every file through the chunked endpoints, so neither a proxy's
request-body cap (Cloudflare: 100 MB) nor the multipart parser's per-request
file-count limit applies. The single-request endpoints remain for API clients.
"""
import os
import re
import time
from pathlib import Path

from fastapi import HTTPException, UploadFile

from venvy.config import _UPLOAD_TMP_DIR


# Uploads. The UI sends every file through the chunked endpoints below, so
# neither a proxy's request-body cap (Cloudflare: 100 MB) nor the multipart
# parser's per-request file-count limit applies. The single-request endpoints
# (/apps/upload with files, /files/upload) remain for API clients.
_SINGLE_REQUEST_UPLOAD_MAX = 50 * 1024 * 1024


_UPLOAD_CHUNK_MAX = 16 * 1024 * 1024


_UPLOAD_FILE_MAX = int(os.getenv("VENVY_MAX_UPLOAD_MB", "10240")) * 1024 * 1024


_UPLOAD_STALE_SEC = 24 * 3600


_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")


_UPLOAD_COPY_BUF = 1024 * 1024


def _save_upload_file(file: UploadFile, dest: Path, limit: int) -> bool:
    """Stream *file* to *dest* without holding it in memory. Returns False (and
    removes the partial file) if it is larger than *limit* bytes."""
    written = 0
    file.file.seek(0)
    with open(dest, "wb") as out:
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
    return True


def _purge_stale_upload_parts() -> None:
    cutoff = time.time() - _UPLOAD_STALE_SEC
    try:
        for part in _UPLOAD_TMP_DIR.glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _upload_part_path(app_id: int, user_id: int, upload_id: str) -> Path:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(400, "Invalid upload_id.")
    return _UPLOAD_TMP_DIR / f"{app_id}-{user_id}-{upload_id}.part"
