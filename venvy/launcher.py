"""Apps as systemd services of their own.

When Venvy runs as root on a systemd host, each app process is a transient
service (``venvy-app-<id>.service``) instead of a child of Venvy:

* It survives Venvy itself: restarting, updating or crashing Venvy leaves
  the apps running; the new Venvy adopts them (same PID and start time).
* systemd runs it as the app's user and applies the per-app limits
  (memory, CPU, tasks, I/O weight, open files), the stop signal and grace
  period, and the sandbox: a read-only system, private /tmp and devices,
  no view of other processes or of Venvy's data (only the app's own folder,
  state and volumes are mounted), no privilege escalation, a system-call
  filter, and — when asked — no network except localhost and an allow-list.
* Output goes to the journal, which keeps it while Venvy is down; Venvy
  follows it into the app's log file (with the journal's own timestamps)
  and remembers where it was.
* How it ended is written by ExecStopPost (as root, outside the app's reach)
  and read by Venvy when it notices the process is gone.

Without systemd (another OS, a container, an unprivileged Venvy) process.py
starts apps as its own children, as before.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from venvy.config import DATA_DIR, ASSETS_DIR
from venvy.isolation import _resolve_system_bin

RUN_DIR = Path(os.getenv("VENVY_RUN_DIR", "/run/venvy"))
ENV_DIR = RUN_DIR / "env"
EXIT_DIR = RUN_DIR / "exit"
_EXEC_ENV = ASSETS_DIR / "venvy" / "runtime_assets" / "exec_env.py"
_DISABLED = os.getenv("VENVY_APP_SERVICES", "1") == "0"
_SIGNALS = ("SIGTERM", "SIGINT", "SIGQUIT", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGWINCH")

_available: Optional[bool] = None
_version = 0


def unit_name(app_id: int) -> str:
    return f"venvy-app-{app_id}.service"


def systemd_version() -> int:
    global _version
    if not _version:
        try:
            out = subprocess.run([_resolve_system_bin("systemctl"), "--version"], capture_output=True,
                                 text=True, timeout=10).stdout
            m = re.search(r"systemd (\d+)", out)
            _version = int(m.group(1)) if m else 0
        except Exception:
            _version = 0
    return _version


def available() -> bool:
    """Whether apps can run as services here (checked once, by starting one)."""
    global _available
    if _available is None:
        ok = (not _DISABLED and hasattr(os, "geteuid") and os.geteuid() == 0
              and Path("/run/systemd/system").exists()
              and os.path.exists(_resolve_system_bin("systemd-run")))
        if ok and systemd_version() < 240:
            logging.info("systemd is older than 240: apps run as Venvy's children, not as services.")
            ok = False
        if ok:
            try:
                r = subprocess.run([_resolve_system_bin("systemd-run"), "--quiet", "--wait", "--collect",
                                    "--service-type=exec", "-p", "StandardOutput=null", "true"],
                                   capture_output=True, timeout=30)
                ok = r.returncode == 0
                if not ok:
                    logging.warning("systemd-run could not start a test service: %s",
                                    r.stderr.decode(errors="replace").strip())
            except Exception as exc:
                logging.warning("systemd-run test failed: %s", exc)
                ok = False
        _available = ok
        if ok:
            for d in (RUN_DIR, ENV_DIR, EXIT_DIR):
                d.mkdir(mode=0o711 if d != EXIT_DIR else 0o700, parents=True, exist_ok=True)
            logging.info("Apps run as systemd services (venvy-app-<id>.service).")
    return _available


def _protect_home_ok() -> bool:
    """ProtectHome would hide the apps when Venvy's data lives under /home."""
    d = str(DATA_DIR.resolve())
    return not (d.startswith("/home") or d.startswith("/root") or d.startswith("/run/user"))


def write_env_file(app_id: int, env: Dict[str, str], ids: Optional[tuple]) -> Path:
    """The environment for exec_env.py, in a folder only the app's user can
    read. exec_env.py deletes it as soon as it has read it. Written without
    ever following a link the app could have put there."""
    d = ENV_DIR / str(app_id)
    if d.is_symlink():
        d.unlink()
    d.mkdir(mode=0o700, exist_ok=True)
    if ids:
        os.chown(d, ids[0], ids[1], follow_symlinks=False)
    d.chmod(0o700)
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with contextlib.suppress(FileNotFoundError):
            os.unlink("env.json", dir_fd=dfd)
        fd = os.open("env.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=dfd)
        try:
            os.write(fd, json.dumps(env).encode("utf-8"))
            if ids:
                os.fchown(fd, ids[0], ids[1])
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    return d / "env.json"


def _user_name(uid: int) -> str:
    """The user's name, as `systemctl show` and the journal then say it too."""
    import pwd
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError):
        return str(uid)


def properties(app, *, ids: Optional[tuple], wd: Path, signal_name: str, grace: float,
               rw_paths: List[str], ro_paths: List[str], as_root: bool) -> List[str]:
    """The unit properties of an app's service."""
    name = unit_name(app.id)
    props = [
        f"WorkingDirectory={wd}",
        "StandardInput=null", "StandardOutput=journal", "StandardError=journal",
        f"SyslogIdentifier={name[:-8]}",
        "KillMode=control-group",
        f"KillSignal={signal_name if signal_name in _SIGNALS else 'SIGTERM'}",
        f"TimeoutStopSec={max(1, int(grace))}s",
        "SendSIGKILL=yes",
        "Restart=no",
        "ExecStopPost=+/bin/sh -c 'echo \"$${SERVICE_RESULT} $${EXIT_CODE} $${EXIT_STATUS}\" > "
        f"{EXIT_DIR}/{name}'",
    ]
    if systemd_version() >= 240:
        props.append("LogRateLimitIntervalSec=0")
    if ids and not as_root:
        props += [f"User={_user_name(ids[0])}", f"Group={ids[1]}"]
    if app.memory_limit_mb:
        props.append(f"MemoryMax={app.memory_limit_mb}M")
    if app.cpu_limit_percent:
        props.append(f"CPUQuota={app.cpu_limit_percent}%")
    props.append(f"TasksMax={app.pids_limit}" if app.pids_limit else "TasksMax=4096")
    if app.io_weight:
        props.append(f"IOWeight={app.io_weight}")
    if app.nofile_limit:
        props.append(f"LimitNOFILE={app.nofile_limit}")
    if getattr(app, "sandbox", True) and not as_root:
        props += [
            "NoNewPrivileges=yes", "PrivateTmp=yes", "PrivateDevices=yes", "ProtectSystem=strict",
            "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
            "RestrictSUIDSGID=yes", "RestrictRealtime=yes", "LockPersonality=yes",
            "RestrictNamespaces=yes", "SystemCallArchitectures=native",
            "SystemCallFilter=@system-service", "SystemCallErrorNumber=EPERM",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
            "ProtectHostname=yes", "ProtectClock=yes", "ProtectKernelLogs=yes", "UMask=0027",
            # Nothing of Venvy's data is visible but the app's own things.
            f"TemporaryFileSystem={DATA_DIR}:ro",
        ]
        if _protect_home_ok():
            props.append("ProtectHome=yes")
        if systemd_version() >= 247:
            props += ["ProtectProc=invisible", "ProcSubset=pid"]
        for p in rw_paths:
            props += [f"BindPaths={p}", f"ReadWritePaths={p}"]
        for p in ro_paths:
            props.append(f"BindReadOnlyPaths={p}")
    egress = getattr(app, "egress_policy", "allow")
    if egress in ("deny", "allowlist") and not as_root:
        props += ["IPAddressDeny=any", "IPAddressAllow=localhost"]
        if egress == "allowlist":
            for cidr in [c.strip() for c in (app.egress_allow or "").split(",") if c.strip()]:
                props.append(f"IPAddressAllow={cidr}")
    return props


async def _run(*argv: str, timeout: float = 30) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"})
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return subprocess.CompletedProcess(argv, -9, b"", b"timed out")
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


async def clear(app_id: int) -> None:
    """No unit of this name is left from before (stopped and forgotten)."""
    name = unit_name(app_id)
    await _run(_resolve_system_bin("systemctl"), "stop", name, timeout=60)
    await _run(_resolve_system_bin("systemctl"), "reset-failed", name, timeout=10)
    with contextlib.suppress(OSError):
        (EXIT_DIR / name).unlink()


async def start(app, argv: List[str], props: List[str], env_file: Optional[Path]) -> int:
    """Start the service; returns its main PID. Raises RuntimeError."""
    name = unit_name(app.id)
    await clear(app.id)
    py = _resolve_system_bin("python3")
    if env_file is not None:
        exec_argv = [py, "-I", str(_EXEC_ENV), "--file", str(env_file), *argv]
    else:
        exec_argv = list(argv)
    cmd = [_resolve_system_bin("systemd-run"), f"--unit={name}", "--service-type=exec", "--quiet",
           "--collect", "--no-ask-password"]
    for p in props:
        cmd += ["-p", p]
    cmd += ["--", *exec_argv]
    r = await _run(*cmd, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("systemd could not start the app: " + r.stderr.decode(errors="replace").strip()[-800:])
    pid = await main_pid(app.id)
    if not pid:
        # Exited at once (or could not exec); how it ended is in the exit file.
        await asyncio.sleep(0.3)
        pid = await main_pid(app.id)
    return pid or 0


async def main_pid(app_id: int) -> int:
    r = await _run(_resolve_system_bin("systemctl"), "show", unit_name(app_id), "-p", "MainPID", "--value", timeout=10)
    with contextlib.suppress(ValueError):
        return int(r.stdout.decode().strip() or "0")
    return 0


async def is_active(app_id: int) -> bool:
    r = await _run(_resolve_system_bin("systemctl"), "is-active", unit_name(app_id), timeout=10)
    return r.stdout.decode().strip() in ("active", "activating", "deactivating", "reloading")


async def stop(app_id: int, grace: float) -> None:
    await _run(_resolve_system_bin("systemctl"), "stop", unit_name(app_id), timeout=grace + 30)


async def kill(app_id: int, sig: str) -> None:
    await _run(_resolve_system_bin("systemctl"), "kill", f"--signal={sig}", unit_name(app_id), timeout=10)


def exit_code(app_id: int) -> Optional[int]:
    """How the service ended (as Popen reports it: -N for a signal), or None if unknown."""
    f = EXIT_DIR / unit_name(app_id)
    try:
        parts = f.read_text().split()
    except OSError:
        return None
    if len(parts) < 3:
        return None
    _result, code, status = parts[:3]
    if code == "exited":
        with contextlib.suppress(ValueError):
            return int(status)
        return 1
    if code in ("killed", "dumped"):
        import signal as _signal
        sig = getattr(_signal, "SIG" + status, None) if not status.isdigit() else int(status)
        return -int(sig) if sig else -9
    return None


def exit_code_wait(app_id: int, timeout: float = 5.0) -> Optional[int]:
    """exit_code(), giving ExecStopPost a moment: it writes the file only
    after the main process is gone, so a reader that noticed the exit first
    would otherwise see nothing. Blocking."""
    end = time.monotonic() + timeout
    while True:
        code = exit_code(app_id)
        if code is not None or time.monotonic() >= end:
            return code
        time.sleep(0.1)


async def wait_exit_code(app_id: int, timeout: float = 5.0) -> Optional[int]:
    return await asyncio.to_thread(exit_code_wait, app_id, timeout)


# ---------------------------------------------------------------------------
# Logs: journal -> the app's log file
# ---------------------------------------------------------------------------

_followers: Dict[int, asyncio.Task] = {}


def _cursor_file(log_file: Path) -> Path:
    return log_file.with_name(log_file.name + ".cursor")


def follow(app_id: int, log_file: Path, write) -> None:
    """Copy the service's output from the journal into *log_file*, from where
    the last copy stopped (so nothing printed while Venvy was down is lost).
    *write(lines: list[bytes])* appends timestamped lines and rotates."""
    old = _followers.get(app_id)
    if old is not None and not old.done():
        return
    _followers[app_id] = asyncio.create_task(_follow(app_id, log_file, write))


async def unfollow(app_id: int, drain: bool = True) -> None:
    t = _followers.pop(app_id, None)
    if t is None:
        return
    if drain:
        await asyncio.sleep(0.5)       # what the journal still has to deliver
    t.cancel()
    with contextlib.suppress(BaseException):
        await t


async def _follow(app_id: int, log_file: Path, write) -> None:
    cursor_file = _cursor_file(log_file)
    argv = [_resolve_system_bin("journalctl"), f"_SYSTEMD_UNIT={unit_name(app_id)}", "-o", "json",
            "--no-pager", "-f", "--all"]
    cursor = None
    with contextlib.suppress(OSError):
        cursor = cursor_file.read_text().strip() or None
    if cursor:
        argv.append(f"--after-cursor={cursor}")
    else:
        argv += ["--since", "-10s"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                    limit=8 * 1024 * 1024)
        assert proc.stdout is not None
        last_save = 0.0
        loop = asyncio.get_running_loop()
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            msg = entry.get("MESSAGE")
            if isinstance(msg, list):
                msg = bytes(x & 0xFF for x in msg if isinstance(x, int))
            elif isinstance(msg, str):
                msg = msg.encode("utf-8", errors="replace")
            else:
                msg = b""
            ts = entry.get("__REALTIME_TIMESTAMP")
            try:
                when = datetime.datetime.fromtimestamp(int(ts) / 1e6, datetime.timezone.utc)
            except (TypeError, ValueError):
                when = datetime.datetime.now(datetime.timezone.utc)
            if entry.get("_TRANSPORT") == "stdout":
                await write([msg], when)
            cur = entry.get("__CURSOR")
            if cur and loop.time() - last_save > 1.0:
                last_save = loop.time()
                with contextlib.suppress(OSError):
                    cursor_file.write_text(cur)
                cursor = cur
            elif cur:
                cursor = cur
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logging.warning("journal follower for app %s stopped: %s", app_id, exc)
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        if cursor:
            with contextlib.suppress(OSError):
                cursor_file.write_text(cursor)


def forget(app_id: int, log_file: Optional[Path] = None) -> None:
    app_id = int(app_id)
    exit_file = os.path.normpath(os.path.join(str(EXIT_DIR), unit_name(app_id)))
    env_dir = os.path.normpath(os.path.join(str(ENV_DIR), str(app_id)))
    if not exit_file.startswith(str(EXIT_DIR) + os.sep) or not env_dir.startswith(str(ENV_DIR) + os.sep):
        return
    with contextlib.suppress(OSError):
        os.unlink(exit_file)
    shutil.rmtree(env_dir, ignore_errors=True)
    if log_file is not None:
        with contextlib.suppress(OSError):
            _cursor_file(log_file).unlink()
