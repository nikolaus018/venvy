"""Full backups and in-place restore.

A backup is one tar.gz holding a consistent data.db snapshot, the encryption
key and all of apps/. Restoring rolls the live server back to one of them and
re-executes the process.

Both can take minutes, so each reports what it is doing as it goes — see
"Operation status" below. That status lives on the server, not in the browser
tab that started it.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from venvy.audit import audit_log
from venvy.config import APPS_BASE_DIR, BACKUP_DIR, DB_FILE, _VENVY_KEY_FILE, utc_now
from venvy.db import engine
from venvy.models import App, User
from venvy.process import set_restore_in_progress, stop_app_process_logic
from venvy.safefs import is_safe_path
from venvy.uploads import _UPLOAD_ID_RE, _UPLOAD_STALE_SEC


# ---------------------------------------------------------------------------
# Operation status
# ---------------------------------------------------------------------------
# One backup or restore runs at a time, and what it is doing right now is kept
# here rather than in the job that started it: a job id lives in one browser
# tab, so closing that tab used to leave nobody able to tell whether the backup
# was still running. Any admin, in any browser, reads the same status from
# GET /api/system/backup/status.
#
# It is also written to BACKUP_DIR on every step change, because a restore
# ends by re-executing the process — the restarted server needs to be able to
# say "the restore finished" (or that something was cut short by a restart).
_OP_FILE = BACKUP_DIR / ".operation.json"


_OP_STEPS = {
    "backup": (
        ("measure", "Measuring apps and database"),
        ("database", "Snapshotting the database"),
        ("archive", "Archiving app files"),
        ("finish", "Saving the backup"),
    ),
    "restore": (
        ("safety", "Backing up the current state first"),
        ("unpack", "Unpacking and checking the backup"),
        ("stop", "Stopping running apps"),
        ("replace", "Replacing apps, database and key"),
        ("restart", "Restarting Venvy"),
    ),
}


_op: Optional[dict] = None


_op_lock = threading.Lock()


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _persist_operation() -> None:
    """Write the current operation to disk. Caller holds _op_lock."""
    try:
        BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
        tmp = _OP_FILE.with_name(_OP_FILE.name + ".tmp")
        tmp.write_text(json.dumps(_op))
        tmp.chmod(0o600)
        os.replace(tmp, _OP_FILE)
    except OSError as e:
        logging.warning(f"Could not save backup/restore status: {e}")


def _begin_operation(kind: str, username: str, filename: Optional[str] = None) -> None:
    """Claim the single backup/restore slot, or answer 409 if it is taken."""
    global _op
    with _op_lock:
        if _op and _op["status"] == "running":
            what = "A restore" if _op["kind"] == "restore" else "A backup"
            raise HTTPException(409, f"{what} is already running — wait for it to finish.")
        _op = {
            "id": secrets.token_hex(8),
            "kind": kind,
            "status": "running",
            "step": 0,
            "detail": "",
            "percent": None,
            "started_at": utc_now().isoformat(),
            "finished_at": None,
            "started_by": username,
            "filename": filename,
            "size_bytes": None,
            "error": None,
        }
        _persist_operation()


def _op_step(key: str, detail: str = "", percent: Optional[int] = None) -> None:
    """Report progress: which step is running, a line about it, and how far
    along it is (None when that can't be known). Safe from worker threads."""
    with _op_lock:
        if not _op or _op["status"] != "running":
            return
        index = [k for k, _ in _OP_STEPS[_op["kind"]]].index(key)
        changed = index != _op["step"]
        _op.update(step=index, detail=detail, percent=percent)
        if changed:
            _persist_operation()


def _finish_operation(error: Optional[str] = None, **fields) -> None:
    with _op_lock:
        if not _op or _op["status"] != "running":
            return
        _op.update(fields)
        _op.update(status="error" if error else "done", error=error,
                   finished_at=utc_now().isoformat())
        if not error:
            _op.update(detail="", percent=None)
        _persist_operation()


def operation_status() -> Optional[dict]:
    """The running backup/restore, or the last one that ran; None if there
    has never been one. Steps come along as labels so the UI only renders."""
    with _op_lock:
        if not _op:
            return None
        op = dict(_op)
    op["steps"] = [label for _, label in _OP_STEPS[op["kind"]]]
    start = datetime.datetime.fromisoformat(op["started_at"])
    end = datetime.datetime.fromisoformat(op["finished_at"]) if op["finished_at"] else utc_now()
    op["elapsed_sec"] = max(0, int((end - start).total_seconds()))
    return op


def load_operation_state() -> None:
    """Called once at startup. Picks up the status the previous process left
    behind and settles it: a restore that got as far as "Restarting Venvy" has
    finished (this process is the restart); anything else still marked running
    was cut short. Also clears temp files nothing can be using any more."""
    global _op
    try:
        loaded = json.loads(_OP_FILE.read_text())
        if loaded.get("kind") not in _OP_STEPS or not isinstance(loaded.get("step"), int):
            raise ValueError("unrecognised status file")
    except FileNotFoundError:
        loaded = None
    except (OSError, ValueError) as e:
        logging.warning(f"Ignoring unreadable backup/restore status: {e}")
        loaded = None
    with _op_lock:
        _op = loaded
        if _op and _op.get("status") == "running":
            restarting = [k for k, _ in _OP_STEPS["restore"]].index("restart")
            if _op["kind"] == "restore" and _op["step"] == restarting:
                _op.update(status="done", detail="", percent=None)
            else:
                _op.update(status="error", error="Venvy stopped or restarted while this was running, so it did not finish.")
            _op["finished_at"] = utc_now().isoformat()
            _persist_operation()
    if BACKUP_DIR.is_dir():
        for pattern in (".tmp-*", ".restore-staging-*"):
            for leftover in BACKUP_DIR.glob(pattern):
                if leftover.is_dir():
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        leftover.unlink()
        _purge_stale_backup_uploads()


def _app_dir_labels() -> Dict[str, str]:
    """Archive path of each app's directory -> the app's name, so progress
    can say which app is being archived instead of showing a raw path."""
    labels = {}
    with contextlib.suppress(Exception):
        with Session(engine) as session:
            for a in session.exec(select(App)).all():
                with contextlib.suppress(ValueError):
                    rel = Path(a.app_dir).resolve().relative_to(APPS_BASE_DIR.resolve())
                    labels["apps/" + rel.as_posix()] = a.name
    return labels


def _create_full_backup_archive(
    protect: Optional[Path] = None,
    report: Callable[..., None] = lambda key, detail="", percent=None: None,
) -> Path:
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

    *report(step, detail, percent)* is told what is happening as it happens.
    """
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    report("measure")
    # Rough space check: gzip typically halves this; refuse clearly instead
    # of filling the disk halfway through.
    apps_bytes = 0
    for root, _dirs, files in os.walk(APPS_BASE_DIR):
        for n in files:
            with contextlib.suppress(OSError):
                apps_bytes += os.lstat(os.path.join(root, n)).st_size
    raw = apps_bytes + (DB_FILE.stat().st_size if DB_FILE.exists() else 0)
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

    labels = _app_dir_labels()
    archived = 0
    current = ("", "")   # (archive path of the app being added, its name)
    last = None

    def _track(info: tarfile.TarInfo) -> tarfile.TarInfo:
        nonlocal archived, current, last
        if info.isreg():
            archived += info.size
        if info.name != current[0] and not info.name.startswith(current[0] + "/"):
            current = next(((p, n) for p, n in labels.items()
                            if info.name == p or info.name.startswith(p + "/")), current)
        percent = min(99, archived * 100 // apps_bytes) if apps_bytes else None
        if (percent, current[1]) != last:   # not once per file: that is a lock per file
            last = (percent, current[1])
            sizes = f"{_fmt_size(archived)} of {_fmt_size(apps_bytes)}"
            report("archive", f"{current[1]} — {sizes}" if current[1] else sizes, percent)
        return info

    try:
        report("database")
        conn = sqlite3.connect(str(DB_FILE))
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_db),))
        finally:
            conn.close()

        report("archive", "", 0 if apps_bytes else None)
        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(tmp_db, arcname="data.db")
            if _VENVY_KEY_FILE.exists():
                tar.add(_VENVY_KEY_FILE, arcname=_VENVY_KEY_FILE.name)
            if APPS_BASE_DIR.exists():
                tar.add(APPS_BASE_DIR, arcname="apps", filter=_track)
    except Exception as e:
        logging.error(f"Full backup failed: {e}")
        tmp_archive.unlink(missing_ok=True)
        raise HTTPException(500, f"Backup failed: {e}")
    finally:
        tmp_db.unlink(missing_ok=True)

    report("finish")
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


# ---------------------------------------------------------------------------
# Uploaded backups
# ---------------------------------------------------------------------------
# Backups belong off the server, so restoring usually starts with a file that
# is not here any more. It arrives in chunks (same protocol as file uploads),
# is checked, and then joins the list like any backup made on this server.
def _backup_upload_part(user_id: int, upload_id: str) -> Path:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(400, "Invalid upload_id.")
    return BACKUP_DIR / f".upload-{user_id}-{upload_id}.part"


def _purge_stale_backup_uploads() -> None:
    cutoff = utc_now().timestamp() - _UPLOAD_STALE_SEC
    for part in BACKUP_DIR.glob(".upload-*.part"):
        with contextlib.suppress(OSError):
            if part.stat().st_mtime < cutoff:
                part.unlink()


def _adopt_uploaded_backup(part: Path, original_name: str) -> Path:
    """Check that a finished upload is a Venvy backup and move it into the
    backup list. Blocking. Removes the part and raises 400 if it is not one."""
    try:
        with tarfile.open(part, "r:gz") as tar:
            # Venvy writes data.db first, so this normally reads one header.
            found = any(m.isreg() and m.name.lstrip("./") == "data.db" for m in tar)
    except (tarfile.TarError, OSError, EOFError):
        part.unlink(missing_ok=True)
        raise HTTPException(400, "That file is not a Venvy backup — expected the .tar.gz a backup download gives you.")
    if not found:
        part.unlink(missing_ok=True)
        raise HTTPException(400, "That archive has no data.db in it, so it is not a Venvy backup.")

    # Keep the name it was downloaded under (it says when the backup was
    # taken) unless that is taken or it was renamed to something else.
    stamp = utc_now()
    candidates = [original_name] if _FULL_BACKUP_FILENAME_RE.match(original_name) else []
    candidates += [f"full-{(stamp + datetime.timedelta(seconds=i)).strftime('%Y%m%d-%H%M%S')}.tar.gz" for i in range(60)]
    part.chmod(0o600)
    for name in candidates:
        try:
            os.link(str(part), str(BACKUP_DIR / name))
        except FileExistsError:
            continue
        part.unlink(missing_ok=True)
        return BACKUP_DIR / name
    part.unlink(missing_ok=True)
    raise HTTPException(409, "Could not find a free name for the uploaded backup — try again.")


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


async def _backup_job(admin: User) -> dict:
    """The body of POST /system/backup, after _begin_operation("backup")."""
    try:
        dest = await asyncio.to_thread(_create_full_backup_archive, None, _op_step)
        size = dest.stat().st_size
    except BaseException as e:
        _finish_operation(error=_error_text(e))
        raise
    audit_log(admin.username, "system_full_backup", f"path={dest}")
    _finish_operation(filename=dest.name, size_bytes=size)
    return {"ok": True, "path": str(dest), "size_bytes": size}


def _error_text(e: BaseException) -> str:
    if isinstance(e, HTTPException):
        return e.detail if isinstance(e.detail, str) else str(e.detail)
    return str(e) or e.__class__.__name__


async def _restore_job(admin: User, archive_path: Path) -> dict:
    """The body of a restore, after _begin_operation("restore")."""
    try:
        return await _run_restore(admin, archive_path)
    except BaseException as e:
        # Failed: this process carries on, so normal service resumes. On
        # success the flag stays set until _self_restart() replaces us.
        set_restore_in_progress(False)
        _finish_operation(error=_error_text(e))
        raise


async def _run_restore(admin: User, archive_path: Path) -> dict:

    # Safety net: back up the CURRENT state before touching anything, so a
    # restore that turns out to be a mistake can itself be undone. If this
    # fails, abort here — nothing has been touched yet.
    backup_labels = dict(_OP_STEPS["backup"])

    def _safety_progress(key: str, detail: str = "", percent: Optional[int] = None) -> None:
        text = backup_labels[key]
        _op_step("safety", f"{text}: {detail}" if detail else text, percent)

    _op_step("safety")
    try:
        pre_restore = await asyncio.to_thread(_create_full_backup_archive, archive_path, _safety_progress)
    except HTTPException as e:
        raise HTTPException(500, f"Could not create a safety backup before restoring — aborted, nothing was touched: {e.detail}")
    audit_log(admin.username, "system_restore_safety_backup", f"path={pre_restore}")

    staging = BACKUP_DIR / f".restore-staging-{secrets.token_hex(4)}"
    staging.mkdir()

    def _extract():
        _op_step("unpack", "Reading the archive")
        with tarfile.open(archive_path, "r:gz") as tar:
            # Reject anything that would land outside `staging` (a crafted
            # or corrupted archive with "../" entries) before extracting a
            # single byte. tarfile's own filter="data" (PEP 706) covers this
            # on 3.12+, but this file supports older interpreters too, so
            # check explicitly regardless of Python version.
            total = 0
            for member in tar.getmembers():
                target = (staging / member.name).resolve()
                if not is_safe_path(staging, target):
                    raise ValueError(f"Backup archive contains an unsafe path: {member.name}")
                total += member.size if member.isreg() else 0
            unpacked = 0
            last = None

            def _tracking_filter(member, dest_path):
                nonlocal unpacked, last
                unpacked += member.size if member.isreg() else 0
                percent = min(99, unpacked * 100 // total) if total else None
                if percent != last:
                    last = percent
                    _op_step("unpack", f"{_fmt_size(unpacked)} of {_fmt_size(total)}", percent)
                return _restore_tar_filter(member, dest_path)

            _op_step("unpack", f"0 B of {_fmt_size(total)}", 0 if total else None)
            try:
                tar.extractall(staging, filter=_tracking_filter)
            except TypeError:
                tar.extractall(staging)  # Python < 3.12 has no `filter` kwarg

    def _swap():
        _op_step("replace", "Putting the backup's app files in place")
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
        _op_step("replace", "Loading the backup's database")
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
        _op_step("stop")
        with Session(engine) as session:
            apps = session.exec(select(App)).all()
            for i, a in enumerate(apps, 1):
                _op_step("stop", f"{a.name} ({i} of {len(apps)})", (i - 1) * 100 // len(apps))
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
    # The status stays "running" on this last step; the restarted process
    # marks it finished (load_operation_state), since only it can know.
    _op_step("restart", "Starting again on the restored data")
    asyncio.get_running_loop().call_later(3.0, _self_restart)
    return {"ok": True, "restarting": True}
