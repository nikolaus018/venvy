"""Keeping a standby copy of Venvy's database off this host.

Every few minutes (System → Backups → Database replication), a consistent
snapshot of data.db and the encryption key is packed the way full backups are
and sent to the off-site destination of the backups (S3, WebDAV or scp),
overwriting the previous one: venvy-db-latest.tar.gz, plus one per day
(venvy-db-YYYYMMDD.tar.gz) to go back to.

Together with the scheduled full backups (the apps' files) this is the
standby: install Venvy on another host, restore the last full backup, then
the latest database snapshot, and point DNS / the tunnel there. A database
snapshot alone restores settings, users, routes and variables without
touching the apps' files.
"""
import asyncio
import contextlib
import logging
import sqlite3
import tarfile
import time
from pathlib import Path

from venvy import kv, remote_backup
from venvy.config import BACKUP_DIR, DB_FILE, _VENVY_KEY_FILE, utc_now

KEY = "db_replication"         # {"minutes": int}
_state = {"last_ok": None, "last_error": None, "last_size": 0}


def settings() -> dict:
    return kv.get_json(KEY, {"minutes": 0}) or {"minutes": 0}


def status() -> dict:
    return {**settings(), **_state, "remote": bool(remote_backup.load())}


def _pack() -> Path:
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    tmp_db = BACKUP_DIR / f".repl-{time.time_ns()}.db"
    out = BACKUP_DIR / ".repl-latest.tar.gz"
    try:
        conn = sqlite3.connect(str(DB_FILE), timeout=60)
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_db),))
        finally:
            conn.close()
        with tarfile.open(out, "w:gz") as tar:
            tar.add(tmp_db, arcname="data.db")
            if _VENVY_KEY_FILE.exists():
                tar.add(_VENVY_KEY_FILE, arcname=_VENVY_KEY_FILE.name)
        out.chmod(0o600)
        return out
    finally:
        tmp_db.unlink(missing_ok=True)


async def replicate_once() -> str:
    cfg = remote_backup.load()
    if not cfg:
        raise RuntimeError("No off-site destination is set up (System → Backups).")
    path = await asyncio.to_thread(_pack)
    where = await remote_backup.upload(path, cfg, "venvy-db-latest.tar.gz")
    day = utc_now().strftime("%Y%m%d")
    if _state.get("_day") != day:
        await remote_backup.upload(path, cfg, f"venvy-db-{day}.tar.gz")
        _state["_day"] = day
    _state.update(last_ok=utc_now().isoformat(), last_error=None, last_size=path.stat().st_size)
    return where


async def replication_task() -> None:
    last = 0.0
    while True:
        await asyncio.sleep(20)
        minutes = int(settings().get("minutes") or 0)
        if minutes <= 0 or time.monotonic() - last < minutes * 60:
            continue
        last = time.monotonic()
        try:
            await replicate_once()
        except Exception as exc:
            _state["last_error"] = str(exc)[:300] or exc.__class__.__name__
            logging.warning("Database replication failed: %s", _state["last_error"])
