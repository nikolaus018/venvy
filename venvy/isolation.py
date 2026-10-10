"""Per-app Unix users (opt-in: VENVY_APP_USERS=1, Linux, Venvy running as root)."""
import contextlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

from venvy.config import APP_LOGS_DIR, APPS_BASE_DIR, child_base_env
from venvy.models import App


# ---------------------------------------------------------------------------
# Per-app Unix users (opt-in: VENVY_APP_USERS=1, Linux, Venvy running as root)
# ---------------------------------------------------------------------------
# Without this, every hosted app, terminal and pip run shares Venvy's own OS
# user and can read data.db, .venvy_key and every other app. With it, each
# app gets a locked system user `venvy-app-<id>` that owns only its own app
# directory; its process, terminal and package installs run as that user.
# Anything Venvy itself writes as root on an app's behalf (logs, the terminal
# rc file) lives outside the app directory, so an app can't redirect those
# writes with a symlink.
_APP_USERS_REQUESTED = os.getenv("VENVY_APP_USERS", "0") == "1"


_APP_USERS_ACTIVE: Optional[bool] = None


def _isolation_active() -> bool:
    global _APP_USERS_ACTIVE
    if _APP_USERS_ACTIVE is None:
        ok = _APP_USERS_REQUESTED
        if ok and not sys.platform.startswith("linux"):
            logging.error("VENVY_APP_USERS=1 is only supported on Linux — running without per-app users.")
            ok = False
        if ok and os.geteuid() != 0:
            logging.error("VENVY_APP_USERS=1 needs Venvy to run as root — running without per-app users.")
            ok = False
        if ok and not (shutil.which("useradd") and shutil.which("userdel") and shutil.which("setpriv")):
            logging.error("VENVY_APP_USERS=1 needs useradd, userdel and setpriv — running without per-app users.")
            ok = False
        _APP_USERS_ACTIVE = ok
        if ok:
            logging.info("Per-app Unix users: ON")
    return _APP_USERS_ACTIVE


def _app_username(app_id: int) -> str:
    return f"venvy-app-{app_id}"


def _app_ids(app_row: "App") -> Optional[tuple]:
    """(uid, gid) of the app's user, or None when isolation is off."""
    if not _isolation_active() or app_row.id is None:
        return None
    import pwd
    try:
        pw = pwd.getpwnam(_app_username(app_row.id))
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Give *path* and everything under it to uid:gid without ever following
    a symlink. The tree belongs to the app, which can rearrange it while we
    walk: os.fwalk() hands out a directory fd per level and verifies each
    directory it enters is the one it listed, so a directory swapped for a
    symlink mid-walk is skipped instead of leading root's chown elsewhere."""
    os.lchown(path, uid, gid)
    if path.is_dir() and not path.is_symlink():
        for _dirpath, dirs, files, dirfd in os.fwalk(path, follow_symlinks=False):
            for n in dirs + files:
                with contextlib.suppress(FileNotFoundError):
                    os.chown(n, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def _ensure_app_user(app_row: "App") -> Optional[tuple]:
    """Create the app's user if needed and make sure it owns its directory.
    Blocking; returns (uid, gid) or None when isolation is off."""
    if not _isolation_active():
        return None
    name = _app_username(app_row.id)
    ids = _app_ids(app_row)
    if ids is None:
        subprocess.run(
            ["useradd", "--system", "--user-group", "--no-create-home",
             "--home-dir", app_row.app_dir, "--shell", "/usr/sbin/nologin", name],
            check=True, capture_output=True, timeout=30,
        )
        ids = _app_ids(app_row)
        if ids is None:
            raise RuntimeError(f"Could not create user {name}")
    uid, gid = ids
    app_dir = Path(app_row.app_dir)
    # Parents: traversable, not listable, so apps can't enumerate each other.
    for parent in (APPS_BASE_DIR, app_dir.parent):
        with contextlib.suppress(OSError):
            parent.chmod(0o711)
    if app_dir.exists() and app_dir.stat().st_uid != uid:
        _chown_tree(app_dir, uid, gid)
    with contextlib.suppress(OSError):
        app_dir.chmod(0o700)
    return ids


def _delete_app_user(app_id: int) -> None:
    if not _isolation_active():
        return
    with contextlib.suppress(Exception):
        subprocess.run(["userdel", _app_username(app_id)], capture_output=True, timeout=30)


def _chown_app_dir(app_row: "App") -> None:
    """Hand the app's whole directory (back) to its user, after a tool that
    ran on its behalf may have left other owners behind."""
    ids = _app_ids(app_row)
    app_dir = Path(app_row.app_dir)
    if ids and app_dir.exists():
        _chown_tree(app_dir, *ids)


def _as_app_user(app_row: "App", argv: list) -> list:
    """The command line that runs *argv* as the app's user (unchanged when
    isolation is off).

    Done with setpriv rather than subprocess's user=/group= arguments:
    uvloop — the event loop uvicorn uses in production — rejects those
    ("unexpected kwargs: user, group, extra_groups"), which made every venv,
    pip and git run fail under per-app users. setpriv execs the command, so
    the PID the caller gets is the command's own."""
    ids = _app_ids(app_row)
    if not ids:
        return list(argv)
    return ["setpriv", f"--reuid={ids[0]}", f"--regid={ids[1]}", "--clear-groups", "--", *argv]


def _app_tool_env(app_row: "App") -> Dict[str, str]:
    """Environment for venv/pip/cargo runs on an app's behalf."""
    env = child_base_env()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if _app_ids(app_row):
        env.update(HOME=app_row.app_dir, USER=_app_username(app_row.id), LOGNAME=_app_username(app_row.id))
    return env


def _app_log_file(app_row: "App") -> Path:
    """Where the app's log lives. Under isolation it's outside the app dir —
    Venvy writes it as root, and the app owns everything under app_dir."""
    if _isolation_active():
        return APP_LOGS_DIR / str(app_row.id) / "app.log"
    return Path(app_row.app_dir) / "logs" / "app.log"
