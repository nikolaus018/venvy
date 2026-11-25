"""Per-app Unix users (VENVY_APP_USERS=1 — the installer's default — Linux, Venvy running as root)."""
import contextlib
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from venvy import runtimes
from venvy.config import APP_LOGS_DIR, APPS_BASE_DIR, ASSETS_DIR, DATA_DIR, child_base_env
from venvy.models import App


# ---------------------------------------------------------------------------
# Per-app Unix users (VENVY_APP_USERS=1, Linux, Venvy running as root). install.sh
# turns this on for a new install; --shared-user leaves it off. Off when unset,
# so `python main.py` from a checkout keeps working without root.
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
_ROOT_WARNED = False
_user_create_lock = threading.Lock()

_SYSTEM_BIN_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
_EXEC_ENV_HELPER = ASSETS_DIR / "venvy" / "runtime_assets" / "exec_env.py"


def _resolve_system_bin(name: str) -> str:
    """Resolve a system binary from a fixed trusted PATH so an app's PATH
    can never hijack root-privileged wrappers like systemd-run or setpriv."""
    # Never the bare name: exec would look it up in the app's PATH. A path that
    # does not exist makes the launch fail instead.
    return shutil.which(name, path=_SYSTEM_BIN_PATH) or f"/usr/bin/{name}"


def _isolation_active() -> bool:
    global _APP_USERS_ACTIVE, _ROOT_WARNED
    if _APP_USERS_ACTIVE is None:
        ok = _APP_USERS_REQUESTED
        missing_reason = None
        if ok and not sys.platform.startswith("linux"):
            missing_reason = "VENVY_APP_USERS=1 is only supported on Linux."
            ok = False
        if ok and os.geteuid() != 0:
            missing_reason = "VENVY_APP_USERS=1 needs Venvy to run as root."
            ok = False
        if ok and not all(shutil.which(b, path=_SYSTEM_BIN_PATH) for b in ("useradd", "userdel", "setpriv")):
            missing_reason = "VENVY_APP_USERS=1 needs useradd, userdel and setpriv in system directories."
            ok = False
        if missing_reason:
            if os.geteuid() == 0 and sys.platform.startswith("linux"):
                # Fail closed: never silently run hosted apps as root when isolation was requested.
                raise RuntimeError(f"{missing_reason} Refusing to run hosted apps as root.")
            logging.error(f"{missing_reason} Running without per-app users.")
        _APP_USERS_ACTIVE = ok
        if ok:
            logging.info("Per-app Unix users: ON")
        elif not _APP_USERS_REQUESTED and os.geteuid() == 0 and not _ROOT_WARNED:
            _ROOT_WARNED = True
            logging.critical(
                "Venvy is running as root with VENVY_APP_USERS=0: hosted apps and terminals "
                "will run as root. Switch isolation modes only with install.sh --shared-user / --app-users."
            )
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
    a symlink or handing over a foreign hard link. The tree belongs to the app,
    which can rearrange it while we walk: os.fwalk() hands out a directory fd
    per level and verifies each directory it enters is the one it listed, so a
    directory swapped for a symlink mid-walk is skipped instead of leading
    root's chown elsewhere."""
    os.lchown(path, uid, gid)
    if path.is_dir() and not path.is_symlink():
        for _dirpath, dirs, files, dirfd in os.fwalk(path, follow_symlinks=False):
            for n in dirs + files:
                try:
                    st = os.stat(n, dir_fd=dirfd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_nlink > 1 and st.st_uid != uid:
                    logging.warning(f"Skipping foreign hard link during chown: {n!r} (uid={st.st_uid})")
                    continue
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
        with _user_create_lock:
            ids = _app_ids(app_row)
            if ids is None:
                res = subprocess.run(
                    [_resolve_system_bin("useradd"), "--system", "--user-group", "--no-create-home",
                     "--home-dir", app_row.app_dir, "--shell", "/usr/sbin/nologin", name],
                    capture_output=True, timeout=30,
                )
                # Exit code 9 = username already exists (benign race).
                if res.returncode not in (0, 9):
                    raise RuntimeError(
                        f"Could not create user {name}: {res.stderr.decode(errors='replace').strip()}"
                    )
                ids = _app_ids(app_row)
                if ids is None:
                    raise RuntimeError(f"Could not create user {name}")
    uid, gid = ids
    app_dir = Path(app_row.app_dir)
    # Parents: traversable, not listable, so apps can't enumerate each other.
    # That includes the data directory itself — an app's user has to pass
    # through it to reach its own files. What else lives there (data.db, the
    # key, backups, logs) is closed by its own mode, see _lock_down_data_files.
    for parent in (DATA_DIR, APPS_BASE_DIR, app_dir.parent):
        with contextlib.suppress(OSError):
            parent.chmod(0o711)
    if app_dir.exists() and not app_dir.is_symlink() and app_dir.stat().st_uid != uid:
        _chown_tree(app_dir, uid, gid)
    with contextlib.suppress(OSError):
        if not app_dir.is_symlink():
            app_dir.chmod(0o700)
    return ids


def _user_pids(uid: int, include_zombies: bool = False) -> list:
    """PIDs of the processes running as *uid* — by any of their real,
    effective, saved or filesystem ids, from /proc/<pid>/status (the owner of
    /proc/<pid> itself turns into root for a process that made itself
    non-dumpable). Zombies are left out unless asked for: they run nothing
    and no signal can remove them."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            zombie = False
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("State:"):
                    zombie = line.split()[1] == "Z"
                elif line.startswith("Uid:"):
                    if str(uid) in line.split()[1:] and (include_zombies or not zombie):
                        pids.append(int(entry.name))
                    break
        except (OSError, ValueError):
            continue
    return pids


# Dynamic-linker, crypto/TLS, glibc, shell-hook, and systemd/D-Bus variables
# that could alter privileged wrappers or loader/library behaviour.
_DANGEROUS_ENV_EXACT = frozenset({
    "GCONV_PATH", "LOCPATH", "HOSTALIASES", "GETCONF_DIR", "NLSPATH",
    "RESOLV_HOST_CONF", "RES_OPTIONS", "LOCALDOMAIN", "TZDIR",
    "BASH_ENV", "ENV", "CDPATH", "IFS", "SHELLOPTS", "TERMINFO", "TERMINFO_DIRS",
    "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "PYTHONINSPECT",
    "MALLOC_CHECK_", "MALLOC_TRACE",
    "NOTIFY_SOCKET", "TMPDIR",
})
_DANGEROUS_ENV_PREFIXES = (
    "LD_", "DBUS_", "SYSTEMD_", "GLIBC_TUNABLES", "MALLOC_",
    "OPENSSL_", "P11_KIT_", "XDG_", "LISTEN_", "KRB5_", "SSL_", "CURL_", "BASH_FUNC_",
)


def is_dangerous_env_var(name: str) -> bool:
    """True if *name* is a loader, crypto/IPC, or shell-hook variable apps must not set."""
    up = name.upper()
    return (
        up in _DANGEROUS_ENV_EXACT
        or any(up.startswith(p) for p in _DANGEROUS_ENV_PREFIXES)
        or "PYDEVD" in up
        or "DEBUGPY" in up
    )


def strip_dangerous_env_vars(env: dict) -> None:
    """Remove dangerous loader/IPC/hook variables from *env* in place."""
    for key in [k for k in env if is_dangerous_env_var(k)]:
        env.pop(key, None)


def _clean_root_env() -> Dict[str, str]:
    """Minimal trusted environment for root-privileged wrappers (`systemd-run`,
    `setpriv`) so untrusted app variables never reach UID 0 code."""
    return {
        "PATH": _SYSTEM_BIN_PATH,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": "/root",
    }


def _prepare_exec_env(env: Dict[str, str]):
    """Write *env* as JSON to an unlinked inheritable temp file so a post-drop
    helper can load it after privileges are dropped. Returns `(file_obj, fd)`.
    Caller must close `file_obj` after spawning the child."""
    tf = tempfile.TemporaryFile()
    try:
        tf.write(json.dumps({str(k): str(v) for k, v in env.items()}).encode("utf-8"))
        tf.flush()
        tf.seek(0)
        fd = tf.fileno()
        os.set_inheritable(fd, True)
        return tf, fd
    except BaseException:
        tf.close()
        raise


def _exec_env_argv(env_fd: int, argv: List[str], ctty: bool = False) -> List[str]:
    """Command invoking `exec_env.py` to apply the env from *env_fd* and exec *argv*."""
    py = _resolve_system_bin("python3") if _isolation_active() else sys.executable
    flags = ["--ctty"] if ctty else []
    return [py, "-I", str(_EXEC_ENV_HELPER), *flags, str(env_fd), *argv]


def _kill_user_processes(uid: int) -> bool:
    """SIGSTOP then SIGKILL everything running as *uid*. Returns True when none remain."""
    for _ in range(20):
        pids = _user_pids(uid)
        if not pids:
            return True
        # Stop before killing so a fork loop cannot outpace per-PID SIGKILL.
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGSTOP)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
    return not _user_pids(uid)


def _delete_app_user(app_id: int) -> bool:
    """Remove the app's user — and first whatever still runs as it. Stopping
    the app and closing its terminals ends what Venvy started, but not what
    those started on their own (a daemon, a `nohup … &` from the terminal).
    Left running, it would also keep userdel from removing the user; and app
    ids are handed out again, so the next app with this id would get the same
    user, and those processes would have its files.
    Returns True if the user is gone (or isolation is off), False on failure."""
    if not _isolation_active():
        return True
    import pwd
    name = _app_username(app_id)
    try:
        uid = pwd.getpwnam(name).pw_uid
    except KeyError:
        return True
    if uid == 0:
        return False
    cleared = False
    try:
        cleared = _kill_user_processes(uid)
    except Exception as exc:
        logging.error(f"Error stopping processes for user {name} (uid={uid}): {exc}")
    if not cleared:
        logging.error(f"Processes for user {name} (uid={uid}) did not exit; refusing to remove user.")
        return False
    try:
        # Only zombies can be left here, and userdel would count them as busy.
        force = ["--force"] if _user_pids(uid, include_zombies=True) else []
        r = subprocess.run([_resolve_system_bin("userdel"), *force, name], capture_output=True, timeout=30)
        if r.returncode != 0:
            logging.error(f"Could not remove user {name}: {r.stderr.decode(errors='replace').strip()}")
            return False
        return True
    except Exception as exc:
        logging.error(f"Could not remove user {name}: {exc}")
        return False


def _chown_app_dir(app_row: "App") -> None:
    """Hand the app's whole directory (back) to its user, after a tool that
    ran on its behalf may have left other owners behind."""
    ids = _app_ids(app_row)
    app_dir = Path(app_row.app_dir)
    if ids and app_dir.exists() and not app_dir.is_symlink():
        _chown_tree(app_dir, *ids)


def _as_app_user(app_row: "App", argv: list) -> list:
    """The command line that runs *argv* as the app's user (unchanged when
    isolation is off).

    Done with setpriv rather than subprocess's user=/group= arguments:
    uvloop — the event loop uvicorn uses in production — rejects those
    ("unexpected kwargs: user, group, extra_groups"), which made every venv,
    pip and git run fail under per-app users. setpriv execs the command, so
    the PID the caller gets is the command's own. `--no-new-privs` blocks
    setuid/setgid host binaries from elevating privileges."""
    if not _isolation_active():
        return list(argv)
    ids = _app_ids(app_row)
    if not ids:
        raise RuntimeError(f"App user for app {app_row.id} does not exist.")
    return [
        _resolve_system_bin("setpriv"),
        f"--reuid={ids[0]}",
        f"--regid={ids[1]}",
        "--clear-groups",
        "--no-new-privs",
        "--",
        *argv,
    ]


def _app_tool_env(app_row: "App") -> Dict[str, str]:
    """Environment for venv/pip/cargo/npm/... runs on an app's behalf."""
    env = child_base_env()
    env["PATH"] = runtimes.tool_search_path()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.update(runtimes.tool_env(app_row.runtime, Path(app_row.app_dir)))
    if _app_ids(app_row):
        env.update(HOME=app_row.app_dir, USER=_app_username(app_row.id), LOGNAME=_app_username(app_row.id))
    strip_dangerous_env_vars(env)
    return env


def _app_log_file(app_row: "App") -> Path:
    """Where the app's log lives. Under isolation it's outside the app dir —
    Venvy writes it as root, and the app owns everything under app_dir."""
    if _isolation_active():
        return APP_LOGS_DIR / str(app_row.id) / "app.log"
    return Path(app_row.app_dir) / "logs" / "app.log"
