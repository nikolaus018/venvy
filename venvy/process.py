"""App process lifecycle.

Starting and stopping hosted apps (optionally inside a systemd scope and as
the app's own Unix user), streaming their output into log files, the
supervisor loop that applies restart policies, port detection, and the
metrics collector.
"""
import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

import aiofiles
from sqlmodel import Session, select

from venvy.auth import _purge_login_attempts
from venvy.config import (
    APP_LOGS_DIR,
    _PRIVATE_ENV_VARS,
    _VENVY_BOOT_TIME,
    _make_tz_aware,
    psutil,
    utc_now,
)
from venvy.crypto import _decrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.isolation import (
    _app_log_file,
    _app_proc_kwargs,
    _app_username,
    _ensure_app_user,
)
from venvy.jobs import _running_tasks
from venvy.models import App, UserSession


# Tracks all PIDs launched in the current Venvy session to ensure 
# reliable cleanup even if DB state is out of sync. {app_id: pid}
_active_app_pids: Dict[int, int] = {}


# Tracks live resource usage for each app_id. {app_id: {"cpu": float, "memory": int}}
_app_metrics: Dict[int, Dict[str, Union[float, int]]] = {}


# Tracks last global host CPU reading to avoid 0.0% on-demand return.
_host_cpu_percent: float = 0.0


def host_cpu_percent() -> float:
    """Latest host CPU reading from the metrics collector."""
    return _host_cpu_percent


def _psutil_net_connections(kind: str = "tcp"):
    """Wrapper for psutil.net_connections that handles the API change in psutil ≥ 6.0."""
    if psutil is None:
        return []
    try:
        return psutil.net_connections(kind=kind)
    except (AttributeError, Exception):
        return []


def _detect_ports_sync(app_pid: int) -> List[int]:
    """Synchronous function to detect all ports a given PID tree is bound to."""
    pids: List[int] = [app_pid]
    if psutil:
        try:
            pids += [c.pid for c in psutil.Process(app_pid).children(recursive=True)]
        except Exception:
            pass
    else:
        try:
            parent_to_children = {}
            for p in Path("/proc").iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    stat = (p / "stat").read_text().split()
                    ppid = int(stat[3])
                    pid = int(p.name)
                    parent_to_children.setdefault(ppid, []).append(pid)
                except Exception:
                    pass
            queue = [app_pid]
            visited = {app_pid}
            while queue:
                current = queue.pop(0)
                for child in parent_to_children.get(current, []):
                    if child not in visited:
                        visited.add(child)
                        pids.append(child)
                        queue.append(child)
        except Exception:
            pass

    detected_ports: Set[int] = set()

    # ── Primary: /proc fd inode matching ─────────────
    for check_pid in pids:
        try:
            fd_dir = Path(f"/proc/{check_pid}/fd")
            socket_inodes: Set[str] = set()
            for fd_path in fd_dir.iterdir():
                try:
                    link = os.readlink(str(fd_path))
                    if link.startswith("socket:["):
                        socket_inodes.add(link[8:-1])
                except OSError:
                    pass
            if not socket_inodes:
                continue
            for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    for line in Path(tcp_file).read_text().splitlines()[1:]:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        if parts[3] == "0A" and parts[9] in socket_inodes:
                            port_num = int(parts[1].split(":")[1], 16)
                            if port_num > 0:
                                detected_ports.add(port_num)
                except Exception:
                    pass
        except (OSError, PermissionError):
            pass

    # ── Secondary: psutil net_connections ─────────────
    if not detected_ports and psutil:
        pid_set = set(pids)
        for conn in _psutil_net_connections(kind="tcp"):
            if (conn.status == "LISTEN"
                    and conn.laddr
                    and conn.pid is not None
                    and conn.pid in pid_set
                    and conn.laddr.port > 0):
                detected_ports.add(conn.laddr.port)

    return sorted(list(detected_ports))


def find_free_port(start: int = 8001, end: int = 9000) -> Optional[int]:
    """Return the first TCP port in [start, end) that can be bound to."""
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    return None


def _verify_port_free(port: int) -> bool:
    """Return True if the port is not currently bound — used immediately before launch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex(("localhost", port)) != 0


def get_server_ipv4() -> str:
    """Return the LAN IPv4 of this machine (best-effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _build_app_env(db_app: App) -> Dict[str, str]:
    """Build the environment dict for a managed app process."""
    env = os.environ.copy()
    for _private in _PRIVATE_ENV_VARS:
        env.pop(_private, None)
    try:
        app_env = json.loads(_decrypt_secret(db_app.env_json) or "{}")
        if isinstance(app_env, dict):
            for k, v in app_env.items():
                env[str(k)] = str(v)
    except Exception:
        pass
    
    effective_port = None
    try:
        m_ports = json.loads(db_app.manual_ports or "[]")
        if m_ports and isinstance(m_ports, list):
            effective_port = m_ports[0]
    except Exception:
        pass
    
    if not effective_port:
        effective_port = db_app.port

    if effective_port:
        env["APP_PORT"] = str(effective_port)
        env["PORT"] = str(effective_port)
    env["PYTHONUNBUFFERED"] = "1"
    
    venv_bin = str(Path(db_app.venv_path) / "bin")
    cargo_bin = str(Path.home() / ".cargo" / "bin")
    
    existing_path = env.get("PATH", "")
    new_paths = [venv_bin]
    if cargo_bin not in existing_path:
        new_paths.append(cargo_bin)
    
    env["PATH"] = ":".join(new_paths + [existing_path]) if existing_path else ":".join(new_paths)
    
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["VIRTUAL_ENV"] = db_app.venv_path
    
    for _bad in ("BASH_ENV", "ENV", "PYTHONSTARTUP"):
        env.pop(_bad, None)
    for _key in list(env.keys()):
        if "PYDEVD" in _key or "DEBUGPY" in _key:
            env.pop(_key, None)
    return {k: str(v) for k, v in env.items()}


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

_log_tasks: Dict[int, asyncio.Task] = {}


_supervisor_event: Optional[asyncio.Event] = None


def _trigger_supervisor() -> None:
    """Wake up the supervisor task immediately."""
    if _supervisor_event:
        _supervisor_event.set()


def _cancel_log_task(app_id: int) -> None:
    task = _log_tasks.pop(app_id, None)
    if task and not task.done():
        task.cancel()


async def _finish_log_task(app_id: int, timeout: float = 2.0) -> None:
    """Let the app's log streamer drain what the process wrote while it was
    shutting down — its pipe reaches EOF once the process is gone — then make
    sure the task is finished either way."""
    task = _log_tasks.pop(app_id, None)
    if not task or task.done():
        return
    await asyncio.wait({task}, timeout=timeout)
    if not task.done():
        task.cancel()


_LOG_MAX_BYTES = int(os.getenv("VENVY_LOG_MAX_MB", "20")) * 1024 * 1024


_LOG_KEEP_ROTATED = 5


_LOG_MAX_LINE = 64 * 1024


def _rotate_log(log_file: Path) -> None:
    """Move app.log aside as app.log.<ts> and keep only the newest few."""
    target = log_file.with_name(f"{log_file.name}.{time.time_ns()}")
    shutil.move(str(log_file), str(target))
    rotated = sorted(log_file.parent.glob(f"{log_file.name}.*"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old_log in rotated[_LOG_KEEP_ROTATED:]:
        try:
            old_log.unlink()
        except OSError:
            pass


async def log_streamer(stream, log_file: Path) -> None:
    """Copy *stream* into *log_file* line by line with timestamps, rotating
    the file whenever it passes _LOG_MAX_BYTES.

    Reads raw chunks instead of StreamReader.readline(): readline() raises on
    any line over 64 KB, and if this loop dies nothing drains the app's stdout
    pipe any more — the app then blocks forever on its next print(). Over-long
    lines are split instead."""
    pending = b""
    f = None
    try:
        f = await aiofiles.open(log_file, "ab")
        while True:
            chunk = await stream.read(65536)
            if chunk:
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                while len(pending) > _LOG_MAX_LINE:
                    lines.append(pending[:_LOG_MAX_LINE])
                    pending = pending[_LOG_MAX_LINE:]
            else:
                lines = [pending] if pending else []
                pending = b""
            if lines:
                ts = utc_now().strftime("%Y-%m-%d %H:%M:%S").encode()
                await f.write(b"".join(b"[" + ts + b"] " + ln + b"\n" for ln in lines))
                await f.flush()
                try:
                    too_big = os.stat(log_file).st_size > _LOG_MAX_BYTES
                except OSError:
                    too_big = False
                if too_big:
                    await f.close()
                    try:
                        await asyncio.to_thread(_rotate_log, log_file)
                    except Exception as exc:
                        logging.warning(f"Log rotation failed: {exc}")
                    f = await aiofiles.open(log_file, "ab")
            if not chunk:
                break
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logging.error(f"Log streamer error: {exc}")
        # Keep draining so the app never blocks on a full pipe.
        with contextlib.suppress(Exception):
            while await stream.read(65536):
                pass
    finally:
        if f is not None:
            with contextlib.suppress(Exception):
                await f.close()


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* exists and is not a zombie."""
    if psutil:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # EPERM: process exists, owned by root — alive
    except ProcessLookupError:
        return False  # ESRCH: no such process
    except OSError:
        return False


async def _pid_alive_async(pid: int) -> bool:
    """Non-blocking wrapper — runs _pid_alive in a thread executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _pid_alive, pid)


def _proc_start_id(pid: int) -> Optional[str]:
    """A fingerprint of when *pid* started, or None if unknown. Two processes
    that happen to share a PID (one exited, the number was reused) differ."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return "proc:" + stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    if psutil:
        try:
            return f"ps:{psutil.Process(pid).create_time():.2f}"
        except Exception:
            pass
    return None


def _app_pid_alive(app_row: "App") -> bool:
    """True only if app_row.pid is alive AND is still the process Venvy started."""
    if not app_row.pid or not _pid_alive(app_row.pid):
        return False
    if app_row.pid_start:
        return _proc_start_id(app_row.pid) == app_row.pid_start
    return True  # recorded before fingerprints existed — can't tell


_SYSTEMD_AVAILABLE: Optional[bool] = None


def _systemd_run_available() -> bool:
    """Whether apps can be launched in a systemd scope: `systemd-run`/`systemctl`
    exist, systemd is PID 1's init, and — checked by actually creating one —
    this process is allowed to start a transient scope. That last part is not
    a given: as an unprivileged service user it needs polkit authorisation,
    and without it every app launch would fail with "Interactive
    authentication required". Cached after the first check (blocking, up to
    a few seconds; warmed at startup)."""
    global _SYSTEMD_AVAILABLE
    if _SYSTEMD_AVAILABLE is None:
        available = bool(
            shutil.which("systemd-run")
            and shutil.which("systemctl")
            and Path("/run/systemd/system").exists()
        )
        if available:
            try:
                available = subprocess.run(
                    ["systemd-run", "--scope", "--quiet", "true"],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
                ).returncode == 0
            except Exception:
                available = False
        _SYSTEMD_AVAILABLE = available
        if not _SYSTEMD_AVAILABLE:
            logging.warning(
                "systemd-run is not usable here — apps will launch without per-app "
                "CPU/memory limits (expected on non-Linux hosts, in containers without "
                "systemd, or when Venvy runs as an unprivileged user that may not "
                "create systemd scopes)."
            )
    return _SYSTEMD_AVAILABLE


# One lock per app serialises start/stop/restart. Without it a manual start
# racing the supervisor's automatic restart (or a double click) spawned two
# processes, and only the second was tracked — the first ran on unmanaged.
_app_locks: Dict[int, asyncio.Lock] = {}


def _app_lock(app_id: int) -> asyncio.Lock:
    return _app_locks.setdefault(app_id, asyncio.Lock())


async def start_app_process_logic(app_id: int, session: Session) -> App:
    async with _app_lock(app_id):
        return await _start_app_process(app_id, session)


async def _start_app_process(app_id: int, session: Session) -> App:
    """Start the app. Caller holds _app_lock(app_id)."""
    # populate_existing: the caller's session may hold a copy loaded before
    # we got the lock.
    db_app = session.get(App, app_id, populate_existing=True)
    if not db_app:
        raise ValueError("App not found.")

    if db_app.pid and await asyncio.to_thread(_app_pid_alive, db_app):
        db_app.status = "running"
        session.add(db_app)
        session.commit()
        logging.info(f"start_app: '{db_app.name}' already running (PID {db_app.pid})")
        return db_app

    db_app.pid = None
    db_app.pid_start = None

    if not db_app.entrypoint and db_app.runtime != "rust":
        db_app.status = "error"
        session.add(db_app)
        session.commit()
        raise RuntimeError("No entrypoint configured. Set one in the app's Settings tab first.")

    if db_app.runtime == "python":
        venv_python = Path(db_app.venv_path) / "bin" / "python"
        if not venv_python.exists():
            db_app.status = "error"
            session.add(db_app)
            session.commit()
            raise RuntimeError("Virtual environment python not found.")

    app_dir = Path(db_app.app_dir)
    ids = await asyncio.to_thread(_ensure_app_user, db_app)
    log_file = _app_log_file(db_app)
    if ids:
        # Venvy-only: apps must not read each other's logs.
        APP_LOGS_DIR.mkdir(mode=0o700, exist_ok=True)
        APP_LOGS_DIR.chmod(0o700)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if log_file.exists() and log_file.stat().st_size > 5 * 1024 * 1024:
        try:
            await asyncio.to_thread(_rotate_log, log_file)
        except Exception as exc:
            logging.warning(f"Log rotation failed: {exc}")

    if not await asyncio.to_thread(log_file.exists):
        await asyncio.to_thread(log_file.touch)

    if not _try_decrypt_secret(db_app.env_json)[0]:
        # Starting it anyway would run the app without its configuration —
        # against a default database, with no API keys — and look healthy.
        msg = ("This app's environment variables can't be decrypted (the .venvy_key file is missing "
               "or was replaced). Re-enter them in Settings, or restore the key, then start again.")
        with contextlib.suppress(OSError):
            with open(log_file, "a", encoding="utf-8") as lf:
                lf.write(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')}] [venvy] {msg}\n")
        db_app.status = "error"
        session.add(db_app)
        session.commit()
        raise RuntimeError(msg)

    env = _build_app_env(db_app)
    if ids:
        env.update(HOME=str(app_dir), USER=_app_username(app_id), LOGNAME=_app_username(app_id))
    _cancel_log_task(app_id)

    try:
        if db_app.runtime == "python":
            venv_python = Path(db_app.venv_path) / "bin" / "python"
            cmd = [str(venv_python), db_app.entrypoint]
        elif db_app.runtime == "rust":
            cmd = ["cargo", "run"]
        else:
            cmd = [db_app.entrypoint]

        stdin_data = None
        effective_port = db_app.port
        if db_app.port:
            port_free = await asyncio.to_thread(_verify_port_free, db_app.port)
            if not port_free:
                new_port = await asyncio.to_thread(find_free_port)
                if new_port:
                    logging.warning(
                        f"Port {db_app.port} for '{db_app.name}' was taken at launch time; "
                        f"reassigning to {new_port}."
                    )
                    db_app.port = new_port
                    try:
                        m_ports = json.loads(db_app.manual_ports or "[]")
                        if m_ports and m_ports[0] == effective_port:
                            m_ports[0] = new_port
                            db_app.manual_ports = json.dumps(m_ports)
                    except Exception:
                        pass
                    env["APP_PORT"] = str(new_port)
                    env["PORT"] = str(new_port)

        unit_name = f"venvy-app-{app_id}"
        launch_cmd = cmd
        used_systemd_scope = False
        if await asyncio.to_thread(_systemd_run_available):
            scope_cmd = ["systemd-run", "--scope", "--quiet", f"--unit={unit_name}"]
            if db_app.memory_limit_mb:
                scope_cmd += ["-p", f"MemoryMax={db_app.memory_limit_mb}M"]
            if db_app.cpu_limit_percent:
                scope_cmd += ["-p", f"CPUQuota={db_app.cpu_limit_percent}%"]
            if ids:
                scope_cmd += [f"--uid={ids[0]}", f"--gid={ids[1]}"]
            scope_cmd += ["--"] + cmd
            launch_cmd = scope_cmd
            used_systemd_scope = True

        process = await asyncio.create_subprocess_exec(
            *launch_cmd,
            cwd=str(app_dir),
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
            # Under systemd-run, --uid/--gid above does the switch (it must
            # start as root to create the scope); otherwise switch here.
            **({} if used_systemd_scope else _app_proc_kwargs(db_app)),
        )

        if process.stdout:
            task = asyncio.create_task(log_streamer(process.stdout, log_file))
            _log_tasks[app_id] = task

        db_app.pid = process.pid
        db_app.pid_start = await asyncio.to_thread(_proc_start_id, process.pid)
        _active_app_pids[app_id] = process.pid
        db_app.status = "running"
        db_app.last_start_time = utc_now()
        db_app.last_exit_code = None
        db_app.systemd_scope = unit_name if used_systemd_scope else None
        session.add(db_app)
        session.commit()
        logging.info(
            f"App '{db_app.name}' started (PID {process.pid})"
            + (f" under systemd scope '{unit_name}.scope'" if used_systemd_scope else " (no resource isolation)")
        )
        
        async def _reap_process(proc: asyncio.subprocess.Process, _app_id: int):
            try:
                rc = await proc.wait()
                logging.info(f"App {_app_id} (PID {proc.pid}) exited with code {rc}")
                try:
                    with Session(engine) as s:
                        app_to_reap = s.get(App, _app_id)
                        if app_to_reap and app_to_reap.pid == proc.pid:
                            app_to_reap.status = "stopped"
                            app_to_reap.pid = None
                            app_to_reap.pid_start = None
                            app_to_reap.systemd_scope = None
                            app_to_reap.last_exit_code = rc
                            app_to_reap.last_stop_time = utc_now()
                            s.add(app_to_reap)
                            s.commit()
                except Exception as db_exc:
                    logging.error(f"Failed to update DB for reaped App {_app_id}: {db_exc}")
            except Exception as exc:
                logging.error(f"Error while waiting for App {_app_id}: {exc}")
            finally:
                # Only forget our own PID: after a restart this slot already
                # holds the replacement process.
                if _active_app_pids.get(_app_id) == proc.pid:
                    _active_app_pids.pop(_app_id, None)
                _running_tasks.discard(asyncio.current_task())
                    
            _trigger_supervisor()

        reap_task = asyncio.create_task(_reap_process(process, app_id))
        _running_tasks.add(reap_task)
        
        return db_app
    except Exception as exc:
        logging.error(f"Failed to start app {app_id}: {exc}")
        db_app.status = "error"
        session.add(db_app)
        session.commit()
        raise


async def _kill_pid_tree(pid: int) -> None:
    """Kill a process and its entire descendant tree, async-safely without self-killing."""
    def _get_pids_to_kill(target_pid: int) -> List[int]:
        res = [target_pid]
        if psutil:
            try:
                res += [c.pid for c in psutil.Process(target_pid).children(recursive=True)]
            except Exception:
                pass
        else:
            try:
                for p in Path("/proc").iterdir():
                    if p.name.isdigit():
                        try:
                            stat = (p / "stat").read_text().split()
                            if int(stat[3]) == target_pid:
                                res.append(int(p.name))
                        except Exception:
                            pass
            except Exception:
                pass
        return list(set(res))

    async def _wait_all_dead(pids: List[int], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(_pid_alive(p) for p in pids):
                return True
            await asyncio.sleep(0.05)
        return not any(_pid_alive(p) for p in pids)

    async def _sig_pgid(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except Exception:
            pass

    async def _sig_pid(p: int, sig: int) -> None:
        try:
            os.kill(p, sig)
        except Exception:
            pass

    _SIGTERM_GRACE = 1.0
    _SIGKILL_WAIT  = 0.5

    # Snapshot the full descendant tree up front. A wrapper process (npm, a shell
    # script, a dev-server launcher) can die from SIGTERM before its own child
    # does — if we only waited on the leader pid, we'd declare victory and skip
    # SIGKILL while a descendant is still alive and holding the port.
    pids = await asyncio.to_thread(_get_pids_to_kill, pid)

    try:
        pgid = os.getpgid(pid)
        # CRITICAL BUGFIX: Ensure we never call killpg on Venvy's own PGID
        if pgid > 0 and pgid != os.getpgid(0):
            await _sig_pgid(pgid, signal.SIGTERM)
            if await _wait_all_dead(pids, _SIGTERM_GRACE):
                return
            await _sig_pgid(pgid, signal.SIGKILL)
            await _wait_all_dead(pids, _SIGKILL_WAIT)
            return
    except Exception:
        pass

    # Fallback to individual process teardown if session/PGID check failed
    for p in pids:
        await _sig_pid(p, signal.SIGTERM)

    if await _wait_all_dead(pids, _SIGTERM_GRACE):
        return

    for p in pids:
        if _pid_alive(p):
            await _sig_pid(p, signal.SIGKILL)
    await _wait_all_dead(pids, _SIGKILL_WAIT)


async def stop_app_process_logic(app_id: int, session: Session, manual: bool = False) -> None:
    async with _app_lock(app_id):
        await _stop_app_process(app_id, session, manual)


async def _stop_app_process(app_id: int, session: Session, manual: bool = False) -> None:
    """Stop the app. Caller holds _app_lock(app_id)."""
    db_app = session.get(App, app_id, populate_existing=True)
    if not db_app:
        _cancel_log_task(app_id)
        return
    # Recorded even when nothing is running: that is exactly the state of a
    # crash-looping app between two automatic restarts, and Stop has to end
    # the loop there too.
    if manual:
        db_app.last_manually_stopped_at = utc_now()
    if not db_app.pid:
        _cancel_log_task(app_id)
        db_app.status = "stopped"
        session.add(db_app)
        session.commit()
        return

    pid = db_app.pid
    if not await asyncio.to_thread(_app_pid_alive, db_app):
        # Already gone — or the PID now belongs to an unrelated process,
        # which must not be killed. Just record the app as stopped.
        logging.info(f"App '{db_app.name}' is not running (PID {pid} gone or reused)")
        await _finish_log_task(app_id)
        db_app.status = "stopped"
        db_app.pid = None
        db_app.pid_start = None
        db_app.systemd_scope = None
        db_app.last_stop_time = utc_now()
        session.add(db_app)
        session.commit()
        return
    logging.info(f"Stopping app '{db_app.name}' (PID {pid})")

    if db_app.systemd_scope:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "stop", f"{db_app.systemd_scope}.scope",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, sd_stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                logging.debug(
                    f"systemctl stop {db_app.systemd_scope}.scope: "
                    f"{sd_stderr.decode(errors='replace').strip()}"
                )
        except Exception as exc:
            logging.warning(f"systemctl stop failed for {db_app.systemd_scope}.scope: {exc}")

    # Always also run the signal-based teardown as a fallback/complement — the scope
    # unit shares the same PID (systemd-run --scope execs in place), so this is a
    # safe no-op if `systemctl stop` above already tore it down.
    try:
        await _kill_pid_tree(pid)
    except Exception as exc:
        logging.warning(f"Error killing PID {pid}: {exc}")
    # Only now: cancelling the streamer before the kill (as this used to)
    # threw away everything the app printed while handling SIGTERM.
    await _finish_log_task(app_id)

    db_app.status = "stopped"
    db_app.pid = None
    db_app.pid_start = None
    db_app.systemd_scope = None
    db_app.last_stop_time = utc_now()
    db_app.last_exit_code = -15

    session.add(db_app)
    session.commit()


# Crash-loop back-off for automatic restarts: 5s, 10s, 20s … capped at 5 min,
# reset once an app has stayed up for _RESTART_STABLE_SEC. {app_id: (failures, not_before)}
_restart_backoff: Dict[int, tuple] = {}


_RESTART_STABLE_SEC = 60


_RESTART_MAX_DELAY = 300


def _restart_allowed(app_id: int) -> bool:
    """Return True (and book the attempt) if an automatic restart may run now."""
    failures, not_before = _restart_backoff.get(app_id, (0, 0.0))
    now = time.monotonic()
    if now < not_before:
        return False
    delay = min(5 * (2 ** failures), _RESTART_MAX_DELAY)
    _restart_backoff[app_id] = (failures + 1, now + delay)
    if failures >= 3:
        logging.warning(f"App {app_id} keeps crashing — next automatic restart in {delay}s at the earliest.")
    return True


# Set while a restore is running (from the request until the process re-execs,
# or until the restore fails). Starting apps is held off for the duration.
_restore_in_progress = False


def restore_in_progress() -> bool:
    return _restore_in_progress


def set_restore_in_progress(value: bool) -> None:
    global _restore_in_progress
    _restore_in_progress = value


async def supervisor_task() -> None:
    """Background loop: detect crashed apps, restart per policy, auto-detect ports."""
    global _supervisor_event
    _supervisor_event = asyncio.Event()

    _last_session_cleanup  = 0.0
    _last_login_cleanup    = 0.0

    while True:
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_supervisor_event.wait(), timeout=1.0)
            _supervisor_event.clear()

            if _restore_in_progress:
                # The restored data.db describes apps as they were when the
                # backup was taken — "running", with PIDs long gone. Acting on
                # that here would start them moments before Venvy re-execs,
                # cutting off their output pipe and leaving them unsupervised.
                continue

            now_mono = time.monotonic()

            if now_mono - _last_session_cleanup > 600:
                _last_session_cleanup = now_mono
                try:
                    with Session(engine) as _cs:
                        expired = _cs.exec(
                            select(UserSession).where(UserSession.expires_at < utc_now())
                        ).all()
                        for s in expired:
                            _cs.delete(s)
                        if expired:
                            _cs.commit()
                            logging.info(f"Purged {len(expired)} expired session(s)")
                except Exception as _exc:
                    logging.warning(f"Session cleanup error: {_exc}")

            if now_mono - _last_login_cleanup > 120:
                _last_login_cleanup = now_mono
                _purge_login_attempts()

            apps_to_check: List[App] = []
            try:
                with Session(engine) as session:
                    apps_to_check = session.exec(select(App)).all()
            except Exception as e:
                logging.error(f"Failed to fetch apps: {e}")
                await asyncio.sleep(5)
                continue

            for stale_row in apps_to_check:
                # This loop awaits (restarts, port scans), so the snapshot above
                # can be seconds old by the time a given app is reached.
                try:
                    with Session(engine) as session:
                        app_row = session.get(App, stale_row.id)
                except Exception as e:
                    logging.error(f"Failed to fetch app {stale_row.id}: {e}")
                    continue
                if app_row is None:
                    continue
                is_alive = _app_pid_alive(app_row)

                if app_row.status == "running" and not is_alive:
                    died = False
                    async with _app_lock(app_row.id):
                        try:
                            with Session(engine) as update_session:
                                refreshed = update_session.get(App, app_row.id)
                                # Re-check under the lock: a restart may have
                                # replaced this PID while we waited, and its
                                # new process must not be written off as dead.
                                if (refreshed and refreshed.status == "running"
                                        and refreshed.pid == app_row.pid):
                                    refreshed.pid = None
                                    refreshed.pid_start = None
                                    refreshed.systemd_scope = None
                                    refreshed.status = "stopped"
                                    refreshed.last_stop_time = utc_now()
                                    if refreshed.last_exit_code is None:
                                        refreshed.last_exit_code = 1
                                    update_session.add(refreshed)
                                    update_session.commit()
                                    died = True
                        except Exception as db_exc:
                            logging.error(f"Failed to update state for {app_row.name}: {db_exc}")
                    if not died:
                        continue
                    logging.warning(f"App '{app_row.name}' (PID {app_row.pid}) died unexpectedly.")

                    if app_row.restart_policy in ("always", "on-failure"):
                        has_started_this_session = (
                            app_row.last_start_time is not None and
                            _make_tz_aware(app_row.last_start_time) >= _make_tz_aware(_VENVY_BOOT_TIME)
                        )
                        if not has_started_this_session and not app_row.start_on_boot:
                            continue

                        was_manual = (
                            app_row.last_manually_stopped_at is not None
                            and app_row.last_start_time is not None
                            and _make_tz_aware(app_row.last_manually_stopped_at)
                                >= _make_tz_aware(app_row.last_start_time)
                        )
                        if not was_manual and _restart_allowed(app_row.id):
                            logging.info(f"Restarting '{app_row.name}' (policy: {app_row.restart_policy})")
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(app_row.id, restart_session)
                            except Exception as exc:
                                logging.error(f"Restart failed for '{app_row.name}': {exc}")

                elif app_row.status == "running" and is_alive:
                    if (app_row.last_start_time is not None
                            and (utc_now() - _make_tz_aware(app_row.last_start_time)).total_seconds() > _RESTART_STABLE_SEC):
                        _restart_backoff.pop(app_row.id, None)
                    try:
                        detected_ports = await asyncio.to_thread(_detect_ports_sync, app_row.pid)
                        manual_list = []
                        try:
                            manual_list = json.loads(app_row.manual_ports or "[]")
                        except Exception:
                            pass
                        
                        if manual_list:
                            combined = manual_list
                        else:
                            combined = [p for p in detected_ports]
                        
                        dp_json = json.dumps(combined)
                        
                        if app_row.detected_ports != dp_json:
                            with Session(engine) as update_session:
                                refreshed = update_session.get(App, app_row.id)
                                if refreshed:
                                    if not refreshed.port and combined:
                                        refreshed.port = combined[0]
                                    refreshed.detected_ports = dp_json
                                    update_session.add(refreshed)
                                    update_session.commit()
                                    logging.info(f"Ports updated to {combined} for '{app_row.name}'")
                    except Exception as exc:
                        logging.debug(f"Port detect scan failed for '{app_row.name}': {exc}")

                elif (app_row.status == "stopped" 
                        and app_row.restart_policy in ("always", "on-failure")
                        and app_row.last_start_time is not None
                        and (app_row.last_stop_time is None or (utc_now() - _make_tz_aware(app_row.last_stop_time)).total_seconds() >= 5.0)):
                    
                    should_restart = False
                    if app_row.restart_policy == "always":
                        should_restart = True
                    elif app_row.restart_policy == "on-failure" and app_row.last_exit_code != 0:
                        should_restart = True
                        
                    if should_restart:
                        has_started_this_session = (
                            app_row.last_start_time is not None and
                            _make_tz_aware(app_row.last_start_time) >= _make_tz_aware(_VENVY_BOOT_TIME)
                        )
                        if not has_started_this_session and not app_row.start_on_boot:
                            continue

                        was_manual = (
                            app_row.last_manually_stopped_at is not None
                            and _make_tz_aware(app_row.last_manually_stopped_at)
                                >= _make_tz_aware(app_row.last_start_time)
                        )
                        if not was_manual and _restart_allowed(app_row.id):
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(app_row.id, restart_session)
                            except Exception:
                                pass

        except Exception as exc:
            logging.error(f"Supervisor loop error: {exc}")


def _collect_metrics_tick(current_app_ids: list, proc_cache: Dict[int, "psutil.Process"]) -> float:  # type: ignore
    """Blocking psutil work for one metrics-collector tick, run via
    asyncio.to_thread so per-app (and per-child-process) /proc reads don't
    stall the event loop — websocket terminal I/O, HTTP responses, and the
    app-supervisor loop all share that loop with this background task.

    proc_cache holds both app-root and child-process Process objects, keyed
    by pid. psutil.Process.cpu_percent(interval=None) always returns 0.0 on
    an instance's first call (it needs a prior sample to diff against) —
    children used to be re-materialized fresh every tick via p.children(),
    so their CPU never accumulated a real sample and always read as 0.0.
    Caching child Process objects across ticks the same way the root
    already was fixes that."""
    host_cpu = psutil.cpu_percent(interval=None)
    live_pids: set = set()

    for app_id in current_app_ids:
        pid = _active_app_pids.get(app_id)
        if not pid:
            continue
        try:
            if pid not in proc_cache:
                p = psutil.Process(pid)
                p.cpu_percent()
                proc_cache[pid] = p

            p = proc_cache[pid]
            live_pids.add(pid)
            with p.oneshot():
                cpu = p.cpu_percent()
                mem = p.memory_info().rss

            for child in p.children(recursive=True):
                cpid = child.pid
                try:
                    if cpid not in proc_cache:
                        child.cpu_percent()  # prime — first read is unreliable, don't use it
                        proc_cache[cpid] = child
                    cached_child = proc_cache[cpid]
                    live_pids.add(cpid)
                    with cached_child.oneshot():
                        cpu += cached_child.cpu_percent()
                        mem += cached_child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    proc_cache.pop(cpid, None)
                    continue

            _app_metrics[app_id] = {"cpu": round(cpu, 1), "memory": mem}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _app_metrics.pop(app_id, None)
            proc_cache.pop(pid, None)
        except Exception:
            pass

    for pid in list(proc_cache.keys()):
        if pid not in live_pids:
            proc_cache.pop(pid, None)
    return host_cpu


async def metrics_collector_task():
    """Background task to collect CPU/RAM usage for all active app processes."""
    if psutil is None:
        return
    proc_cache: Dict[int, psutil.Process] = {} # type: ignore
    psutil.cpu_percent(interval=None)

    while True:
        try:
            global _host_cpu_percent
            current_app_ids = list(_active_app_pids.keys())
            _host_cpu_percent = await asyncio.to_thread(
                _collect_metrics_tick, current_app_ids, proc_cache
            )
        except Exception as exc:
            logging.error(f"Metrics engine error: {exc}")
        await asyncio.sleep(2.5)
