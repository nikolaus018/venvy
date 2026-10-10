"""Full backups and in-place restore.

A backup is one tar.gz holding a consistent data.db snapshot, the encryption
key and all of apps/. Restoring rolls the live server back to one of them and
re-executes the process.
"""
import asyncio
import contextlib
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from venvy.audit import audit_log
from venvy.config import APPS_BASE_DIR, BACKUP_DIR, DB_FILE, _VENVY_KEY_FILE, utc_now
from venvy.db import engine
from venvy.models import App, User
from venvy.process import set_restore_in_progress, stop_app_process_logic
from venvy.safefs import is_safe_path


def _create_full_backup_archive(protect: Optional[Path] = None) -> Path:
    """Build a complete disaster-recovery backup — a consistent data.db
    snapshot (via SQLite's VACUUM INTO, safe on a live WAL-mode database),
    the .venvy_key encryption key, and the entire apps/ directory (code,
    venvs, logs) — bundled into one tar.gz in BACKUP_DIR. This is the only
    format the UI creates going forward: one file covers everything needed
    to restore Venvy, so backing up (and restoring) never requires touching
    the server's filesystem directly.

    Raises HTTPException on failure. Returns the path to the finished
    archive on success. Blocking (tars the whole apps/ dir) — callers on
    the event loop should run this via asyncio.to_thread; sync route
    handlers already run off the event loop via Starlette's threadpool.
    """
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    # Rough space check: gzip typically halves this; refuse clearly instead
    # of filling the disk halfway through.
    raw = DB_FILE.stat().st_size if DB_FILE.exists() else 0
    for root, _dirs, files in os.walk(APPS_BASE_DIR):
        for n in files:
            with contextlib.suppress(OSError):
                raw += os.lstat(os.path.join(root, n)).st_size
    free = shutil.disk_usage(BACKUP_DIR).free
    if raw // 2 + 256 * 1024 * 1024 > free:
        raise HTTPException(507, f"Not enough free disk space for a backup (≈{raw // 2 // (1024 * 1024)} MB needed, {free // (1024 * 1024)} MB free).")
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"full-{timestamp}.tar.gz"

    # Same "write under a name only this request knows, then hard-link into
    # place" trick the old DB-only backup used — a concurrent request that
    # loses the race on this second-resolution timestamp can only ever
    # delete its OWN temp files, never a different request's completed one.
    suffix = secrets.token_hex(4)
    tmp_db = BACKUP_DIR / f".tmp-{timestamp}-{suffix}.db"
    tmp_archive = BACKUP_DIR / f".tmp-{timestamp}-{suffix}.tar.gz"

    try:
        conn = sqlite3.connect(str(DB_FILE))
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_db),))
        finally:
            conn.close()

        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(tmp_db, arcname="data.db")
            if _VENVY_KEY_FILE.exists():
                tar.add(_VENVY_KEY_FILE, arcname=_VENVY_KEY_FILE.name)
            if APPS_BASE_DIR.exists():
                tar.add(APPS_BASE_DIR, arcname="apps")
    except Exception as e:
        logging.error(f"Full backup failed: {e}")
        tmp_archive.unlink(missing_ok=True)
        raise HTTPException(500, f"Backup failed: {e}")
    finally:
        tmp_db.unlink(missing_ok=True)

    try:
        os.link(str(tmp_archive), str(dest))
    except FileExistsError:
        raise HTTPException(409, "A backup for this exact timestamp already exists — try again.")
    finally:
        tmp_archive.unlink(missing_ok=True)
    dest.chmod(0o600)

    # Retention: keep the newest _BACKUP_KEEP full backups.
    fulls = sorted(BACKUP_DIR.glob("full-*.tar.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in fulls[_BACKUP_KEEP:]:
        if protect is not None and old.name == protect.name:
            continue  # e.g. the backup a restore is about to read
        with contextlib.suppress(OSError):
            old.unlink()
            logging.info(f"Pruned old backup {old.name}")
    return dest


_BACKUP_KEEP = max(1, int(os.getenv("VENVY_BACKUP_KEEP", "10")))


# (pattern, type label) — "database" is a legacy DB-only snapshot from an
# older Venvy version (download/delete only, not restorable from the UI);
# "full" is the current format, listed so upgrading never orphans old backups.
_BACKUP_TYPES = (
    ("data-*.db", "database"),
    ("full-*.tar.gz", "full"),
)


_BACKUP_FILENAME_RE = re.compile(r"\A(?:data-\d{8}-\d{6}\.db|full-\d{8}-\d{6}\.tar\.gz)\Z")


_FULL_BACKUP_FILENAME_RE = re.compile(r"\Afull-\d{8}-\d{6}\.tar\.gz\Z")


def _resolve_backup_path(filename: str) -> Path:
    """Validate a backup filename against the fixed naming schemes used by
    _create_full_backup_archive() (current) and the retired DB-only
    create_backup() (legacy), resolve it inside BACKUP_DIR, and confirm the
    resolved path is actually still inside BACKUP_DIR (matches the
    is_safe_path pattern used for app file access elsewhere in this file) —
    rejects anything that isn't an exact, contained match."""
    if not _BACKUP_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid backup filename.")
    path = BACKUP_DIR / filename
    if not is_safe_path(BACKUP_DIR, path) or not path.is_file():
        raise HTTPException(404, "Backup not found.")
    return path


def _restore_tar_filter(member, dest_path):
    """tarfile's "data" filter, except for symlinks: every venv links
    bin/python3 to the system interpreter (and other names to that link),
    which "data" rejects as pointing outside the archive — so restoring any
    Python app used to fail. A symlink by itself writes nothing; the "tar"
    filter still refuses any later member whose real path (following links
    extracted earlier) would land outside the staging directory."""
    if member.issym():
        return tarfile.tar_filter(member, dest_path)
    if not (member.isreg() or member.isdir() or member.islnk()):
        # FIFOs, sockets, devices. "data" doesn't skip these, it aborts the
        # whole extraction — so one named pipe in one app's directory made
        # every backup taken since unrestorable. Nothing in them to restore.
        logging.warning(f"Restore: skipping special file {member.name}")
        return None
    return tarfile.data_filter(member, dest_path)


def _self_restart():
    """Replace this process's image with a fresh invocation of itself
    (same PID, same argv/cwd/env) so every piece of in-memory state — the
    DB engine's connections, the encryption key cache, active-process
    tracking, background tasks — reinitializes cleanly against whatever is
    now on disk. Used after a restore, where swapping data.db/apps/the key
    out from under a live process is otherwise fragile."""
    logging.warning("Restarting Venvy to complete the restore...")
    # A frozen build is its own executable: argv[0] is already the binary, and
    # prepending it again would hand the path to argparse as a stray argument,
    # so the restarted process exited instead of coming back up.
    argv = sys.argv if getattr(sys, "frozen", False) else [sys.executable] + sys.argv
    os.execv(sys.executable, argv)


async def _restore_job(admin: User, archive_path: Path) -> dict:
    try:
        return await _run_restore(admin, archive_path)
    except BaseException:
        # Failed: this process carries on, so normal service resumes. On
        # success the flag stays set until _self_restart() replaces us.
        set_restore_in_progress(False)
        raise


async def _run_restore(admin: User, archive_path: Path) -> dict:

    # Safety net: back up the CURRENT state before touching anything, so a
    # restore that turns out to be a mistake can itself be undone. If this
    # fails, abort here — nothing has been touched yet.
    try:
        pre_restore = await asyncio.to_thread(_create_full_backup_archive, archive_path)
    except HTTPException as e:
        raise HTTPException(500, f"Could not create a safety backup before restoring — aborted, nothing was touched: {e.detail}")
    audit_log(admin.username, "system_restore_safety_backup", f"path={pre_restore}")

    staging = BACKUP_DIR / f".restore-staging-{secrets.token_hex(4)}"
    staging.mkdir()

    def _extract():
        with tarfile.open(archive_path, "r:gz") as tar:
            # Reject anything that would land outside `staging` (a crafted
            # or corrupted archive with "../" entries) before extracting a
            # single byte. tarfile's own filter="data" (PEP 706) covers this
            # on 3.12+, but this file supports older interpreters too, so
            # check explicitly regardless of Python version.
            for member in tar.getmembers():
                target = (staging / member.name).resolve()
                if not is_safe_path(staging, target):
                    raise ValueError(f"Backup archive contains an unsafe path: {member.name}")
            try:
                tar.extractall(staging, filter=_restore_tar_filter)
            except TypeError:
                tar.extractall(staging)  # Python < 3.12 has no `filter` kwarg

    def _swap():
        if (staging / "apps").is_dir():
            if APPS_BASE_DIR.exists():
                shutil.rmtree(APPS_BASE_DIR)
            shutil.move(str(staging / "apps"), str(APPS_BASE_DIR))
        # The database is loaded *into* the live file through SQLite's backup
        # API, not swapped underneath it. Requests keep arriving during a
        # restore (the UI polls this very job), and replacing data.db while
        # deleting its -wal/-shm by hand left two windows in which a
        # connection could pair the new database with the old one's WAL —
        # which SQLite would then replay onto it. The backup API takes the
        # proper locks and goes through the WAL like any other write.
        engine.dispose()
        src = sqlite3.connect(str(staging / "data.db"))
        try:
            dst = sqlite3.connect(str(DB_FILE), timeout=60)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        engine.dispose()
        restored_key = staging / _VENVY_KEY_FILE.name
        if restored_key.is_file():
            shutil.move(str(restored_key), str(_VENVY_KEY_FILE))
            _VENVY_KEY_FILE.chmod(0o600)

    try:
        # Unpack and check the archive first: if it is unusable, nothing has
        # been stopped or changed yet.
        try:
            await asyncio.to_thread(_extract)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except (tarfile.TarError, OSError, EOFError) as e:
            raise HTTPException(400, f"Backup archive could not be read — restore aborted, nothing was changed: {e}")
        if not (staging / "data.db").is_file():
            raise HTTPException(400, "Backup archive is missing data.db — restore aborted, nothing was changed.")

        # Stop every running app before touching apps/ or data.db out from under them.
        with Session(engine) as session:
            for a in session.exec(select(App)).all():
                try:
                    await stop_app_process_logic(a.id, session, manual=True)
                except Exception as e:
                    logging.warning(f"Failed to stop app {a.id} before restore: {e}")

        await asyncio.to_thread(_swap)
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)

    audit_log(admin.username, "system_restore_completed", f"path={archive_path}")

    # Venvy's in-memory state is now stale relative to what's on disk —
    # restart so everything reinitializes cleanly. Delay briefly so this
    # response actually reaches the client before the process image swaps.
    # Give the client a few polls to see "done" before the process swaps.
    asyncio.get_running_loop().call_later(3.0, _self_restart)
    return {"ok": True, "restarting": True}
