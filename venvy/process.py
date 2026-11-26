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
import sys
import threading
import time
import types
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

import aiofiles
from sqlmodel import Session, select

from venvy.auth import _purge_login_attempts, _purge_mutation_hits
from venvy.config import (
    APP_LOGS_DIR,
    DATA_DIR,
    child_base_env,
    _VENVY_BOOT_TIME,
    _make_tz_aware,
    psutil,
    utc_now,
)
from venvy.crypto import _decrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.isolation import (
    _app_log_file,
    _app_tool_env,
    _as_app_user,
    _app_username,
    _clean_root_env,
    _ensure_app_user,
    _exec_env_argv,
    _isolation_active,
    _prepare_exec_env,
    _resolve_system_bin,
    strip_dangerous_env_vars,
)
from venvy import monitor, notify, runtimes, shared_env
from venvy.jobs import _running_tasks
from venvy.models import App, UserSession


DOCKER_ENV_DIR = DATA_DIR / ".docker-env"
# Every web process gets a $PORT when it has none (an app that listens on a
# port of its own is followed there by port detection).
_AUTO_PORT = os.getenv("VENVY_AUTO_PORT", "1") == "1"


def vars_of(obj) -> dict:
    """The fields of an App row or of an effective() namespace, as a dict."""
    if isinstance(obj, types.SimpleNamespace):
        return dict(vars(obj))
    return obj.model_dump()


# Tracks all PIDs launched in the current Venvy session to ensure 
# reliable cleanup even if DB state is out of sync. {app_id: pid}
_active_app_pids: Dict[int, int] = {}


# Tracks live resource usage for each app_id. {app_id: {"cpu": float, "memory": int}}
_app_metrics: Dict[int, Dict[str, Union[float, int]]] = {}
# What docker / compose apps' containers use (from `docker stats`, see telemetry):
# their CLI process, which is what Venvy started, does not do the work.
container_usage: Dict[int, Dict[str, Union[float, int]]] = {}


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
                    rest = (p / "stat").read_text().rsplit(")", 1)[1].split()
                    ppid = int(rest[1])
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
    socket_inodes: Set[str] = set()
    for check_pid in pids:
        try:
            for fd_path in Path(f"/proc/{check_pid}/fd").iterdir():
                try:
                    link = os.readlink(str(fd_path))
                    if link.startswith("socket:["):
                        socket_inodes.add(link[8:-1])
                except OSError:
                    pass
        except (OSError, PermissionError):
            pass
    if socket_inodes:
        # The table is the same whichever process asks: read it once per
        # scan, not once per process in the tree.
        for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                for line in Path(tcp_file).read_text().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    if parts[3] == "0A" and parts[9] in socket_inodes:
                        port_num = int(parts[1].rsplit(":", 1)[1], 16)
                        if port_num > 0:
                            detected_ports.add(port_num)
            except Exception:
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


_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
_DOCKER_CLI_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin"}


def _container_host_ports_sync(app_row: "App") -> List[int]:
    """The host ports a docker / compose app's own containers publish. Docker
    listens on those itself (docker-proxy, or only iptables rules), so they
    are not in the app's process tree, but they are the app's: its project
    (compose) or its container (docker), and compose_guard keeps any other
    app from taking that project's label or that container's name."""
    if getattr(app_row, "runtime", "") not in ("docker", "compose") or not shutil.which("docker"):
        return []
    name = runtimes.docker_name(app_row.id)
    filt = f"label={_COMPOSE_PROJECT_LABEL}={name}" if app_row.runtime == "compose" else f"name=^{name}$"
    try:
        ids = subprocess.run(["docker", "ps", "-q", "--no-trunc", "--filter", filt], capture_output=True,
                             text=True, timeout=20, env=_DOCKER_CLI_ENV, stdin=subprocess.DEVNULL)
        if ids.returncode != 0 or not ids.stdout.split():
            return []
        out = subprocess.run(["docker", "inspect", *ids.stdout.split()], capture_output=True, text=True,
                             timeout=20, env=_DOCKER_CLI_ENV, stdin=subprocess.DEVNULL)
        containers = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    ports: Set[int] = set()
    for c in containers if isinstance(containers, list) else []:
        if not isinstance(c, dict):
            continue
        labels = (c.get("Config") or {}).get("Labels") or {}
        if app_row.runtime == "compose" and labels.get(_COMPOSE_PROJECT_LABEL) != name:
            continue
        if app_row.runtime == "docker" and (_COMPOSE_PROJECT_LABEL in labels or c.get("Name") != f"/{name}"):
            continue
        for bindings in ((c.get("NetworkSettings") or {}).get("Ports") or {}).values():
            for b in bindings or []:
                with contextlib.suppress(TypeError, ValueError, AttributeError):
                    p = int(b.get("HostPort"))
                    if 0 < p < 65536:
                        ports.add(p)
    return sorted(ports)


_port_alloc_lock = threading.Lock()
_recent_allocated_ports: Dict[int, float] = {}


def _listening_ports() -> Set[int]:
    """Every TCP port something on the host listens on, on any interface and
    address family: psutil, else Linux's /proc/net/tcp{,6} (state 0A = LISTEN)."""
    ports: Set[int] = set()
    for conn in _psutil_net_connections(kind="tcp"):
        if getattr(conn, "status", None) == "LISTEN" and conn.laddr:
            ports.add(conn.laddr.port)
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, "r") as f:
                next(f, None)
                for line in f:
                    fields = line.split()
                    if len(fields) >= 4 and fields[3] == "0A":
                        with contextlib.suppress(ValueError, IndexError):
                            ports.add(int(fields[1].rsplit(":", 1)[1], 16))
        except OSError:
            continue
    return ports


def find_free_port(start: int = 8001, end: int = 9000) -> Optional[int]:
    """Return the first TCP port in [start, end) that nothing listens on,
    serialized across concurrent launches so two apps don't claim the same port.
    A listener on any interface is found by the scan; the loopback bind also
    catches one on the wildcard address (and anything the scan can't see)."""
    now = time.monotonic()
    with _port_alloc_lock:
        for p in [k for k, ts in _recent_allocated_ports.items() if now - ts > 10.0]:
            _recent_allocated_ports.pop(p, None)
        busy = _listening_ports()
        for port in range(start, end):
            if port in _recent_allocated_ports or port in busy:
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("127.0.0.1", port))
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                    s2.settimeout(0.2)
                    if s2.connect_ex(("127.0.0.1", port)) == 0:
                        continue
                _recent_allocated_ports[port] = now
                return port
            except OSError:
                continue
    return None


def _verify_port_free(port: int) -> bool:
    """Return True if the port is not currently bound — used immediately before launch."""
    for family, addr in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((addr, port)) == 0:
                    return False
        except OSError:
            continue
    return True


def get_server_ipv4() -> str:
    """Return the LAN IPv4 of this machine (best-effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _build_app_env(db_app: App, spec=None, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Build the environment dict for a managed app process.

    In order, later ones winning: Venvy's own (minus its settings), the
    variables shared by every app, the connection variables of the services
    the app uses, the parent's variables (for a worker or replica), the app's
    own, *extra* (secrets from SOPS / Vault), then what Venvy itself sets
    (PORT, paths)."""
    from venvy import appspec, services, toolchains, volumes
    spec = spec if spec is not None else db_app
    env = child_base_env()
    env.update(shared_env.load())      # for every app; the app's own variables below win
    with contextlib.suppress(Exception):
        env.update(services.binding_env(appspec.owner_id(spec)))
    sources = [db_app.env_json]
    if getattr(db_app, "parent_app_id", None):
        with contextlib.suppress(Exception):
            with Session(engine) as s:
                parent = s.get(App, db_app.parent_app_id)
                if parent is not None:
                    sources.insert(0, parent.env_json)
    for src in sources:
        try:
            app_env = json.loads(_decrypt_secret(src) or "{}")
            if isinstance(app_env, dict):
                for k, v in app_env.items():
                    env[str(k)] = str(v)
        except Exception:
            pass
    if extra:
        env.update(extra)

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
    if spec.runtime == "docker" and effective_port:
        env["PORT"] = str(spec.container_port or 8080)     # inside the container
    env["PYTHONUNBUFFERED"] = "1"
    wd = appspec.work_dir(spec)
    run_vars, run_path = runtimes.run_env(spec.runtime, wd, effective_port)
    for _k, _v in run_vars.items():
        env.setdefault(_k, _v)      # the app's own settings win
    env.update(appspec.app_paths_env(spec))
    with contextlib.suppress(Exception):
        env.update(volumes.volume_env(appspec.owner_id(spec)))
    env.update(toolchains.tool_env(spec, wd))

    venv_bin = str(Path(spec.venv_path) / "bin")
    cargo_bin = str(Path.home() / ".cargo" / "bin")

    existing_path = env.get("PATH", "")
    new_paths = toolchains.path_dirs(spec, wd) + run_path + [venv_bin]
    if cargo_bin not in existing_path and not _isolation_active():
        new_paths.append(cargo_bin)

    env["PATH"] = ":".join(new_paths + [existing_path]) if existing_path else ":".join(new_paths)

    strip_dangerous_env_vars(env)
    env["VIRTUAL_ENV"] = spec.venv_path
    return {k: str(v) for k, v in env.items()}


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

_log_tasks: Dict[int, asyncio.Task] = {}


_supervisor_event: Optional[asyncio.Event] = None


def _routes_changed() -> None:
    """A process started or stopped: the proxy must not wait for its table to expire."""
    from venvy import routing
    routing.invalidate()


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
    entries = []
    for p in log_file.parent.glob(f"{log_file.name}.*"):
        with contextlib.suppress(OSError):
            entries.append((p.stat().st_mtime, p))
    entries.sort(key=lambda x: x[0], reverse=True)
    for _, old_log in entries[_LOG_KEEP_ROTATED:]:
        with contextlib.suppress(OSError):
            old_log.unlink()


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
                from venvy import logship
                logship.offer(log_file, lines)
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
        sr = _resolve_system_bin("systemd-run")
        sc = _resolve_system_bin("systemctl")
        available = bool(
            os.path.exists(sr)
            and os.path.exists(sc)
            and Path("/run/systemd/system").exists()
        )
        if available:
            try:
                available = subprocess.run(
                    [sr, "--scope", "--quiet", "--no-ask-password", "true"],
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


async def _run_systemctl(*args: str, timeout: float = 10) -> None:
    """Run `systemctl <args>`, best effort: the outcome is logged, never
    raised, and a call that hangs is killed rather than left behind."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _resolve_system_bin("systemctl"), *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        logging.warning(f"systemctl {' '.join(args)} could not be run: {exc}")
        return
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logging.warning(f"systemctl {' '.join(args)} timed out after {timeout:g}s")
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return
    if proc.returncode != 0:
        logging.debug(f"systemctl {' '.join(args)}: {stderr.decode(errors='replace').strip()}")


async def _clear_stale_scope(unit_name: str) -> None:
    """Make sure no scope called *unit_name* is left over from an earlier
    run of the app — whatever is still in it belongs to a process that has
    already ended or been stopped. A few quick no-op systemctl calls when
    there is none."""
    await _run_systemctl("kill", "--signal=SIGKILL", f"{unit_name}.scope", timeout=5)
    await _run_systemctl("stop", f"{unit_name}.scope")
    await _run_systemctl("reset-failed", f"{unit_name}.scope", timeout=5)


# One lock per app serialises start/stop/restart. Without it a manual start
# racing the supervisor's automatic restart (or a double click) spawned two
# processes, and only the second was tracked — the first ran on unmanaged.
_app_locks: Dict[int, asyncio.Lock] = {}


def _app_lock(app_id: int) -> asyncio.Lock:
    return _app_locks.setdefault(app_id, asyncio.Lock())


async def start_app_process_logic(app_id: int, session: Session) -> App:
    async with _app_lock(app_id):
        return await _start_app_process(app_id, session)


async def restart_app_process_logic(app_id: int) -> None:
    """Stop, then start. One lock hold for both halves, so nothing else can
    start the app in the gap between them."""
    async with _app_lock(app_id):
        with Session(engine) as s:
            await _stop_app_process(app_id, s, manual=False)
        with Session(engine) as s:
            await _start_app_process(app_id, s)


def _venvy_log(log_file: Path, msg: str) -> None:
    """A line from Venvy itself in the app's log."""
    with contextlib.suppress(OSError):
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')}] [venvy] {msg}\n")


def forget_app_runtime(app_id: int) -> None:
    """Clean up all in-memory process/metrics/backoff state when an app is deleted."""
    _active_app_pids.pop(app_id, None)
    _app_metrics.pop(app_id, None)
    _cpu_seen.pop(app_id, None)
    _restart_backoff.pop(app_id, None)
    _last_port_scan.pop(app_id, None)
    _cancel_log_task(app_id)
    container_usage.pop(app_id, None)
    # The app's variables, as handed to docker / compose: nothing to keep them for.
    from venvy import compose_guard
    compose_guard.forget(runtimes.docker_name(app_id))
    with contextlib.suppress(OSError):
        (DOCKER_ENV_DIR / f"{app_id}.env").unlink()
    lock = _app_locks.get(app_id)
    if lock is not None and not lock.locked():
        _app_locks.pop(app_id, None)


# Build output goes to the app's log as it comes (a cold cargo or npm build
# can run for many minutes); past this much only the end is kept, at the end.
_PRESTART_LOG_MAX = 2 * 1024 * 1024
_PRESTART_TAIL_MAX = 64 * 1024


async def _run_prestart_step(db_app, step: "runtimes.Step", log_file: Path, first_run: bool = True) -> None:
    """Run a dependency install or build before the start, with its output in
    the app's log. Raises if it fails — the app would only crash without it."""
    from venvy import appspec
    if first_run:
        _venvy_log(log_file, f"Not installed or built yet — running {step.what}…")
    else:
        _venvy_log(log_file, f"Running {step.what}…")
    tool_env = _app_tool_env(db_app)
    tool_env.update(dict(step.env))
    wd = appspec.work_dir(db_app)
    env_tf = None
    try:
        if step.as_venvy:
            # docker build: the daemon is root's; the app's variables stay out of it,
            # and a compose file runs only as checked (compose_guard).
            from venvy import compose_guard
            try:
                argv = await asyncio.to_thread(compose_guard.pinned_argv, db_app, list(step.argv))
            except compose_guard.ComposeRefused as exc:
                _venvy_log(log_file, str(exc))
                raise RuntimeError(str(exc))
            spawn_env = _clean_root_env()
            spawn_env["DOCKER_BUILDKIT"] = "1"
            pass_fds = ()
        elif _isolation_active():
            env_tf, env_fd = _prepare_exec_env(tool_env)
            inner = _exec_env_argv(env_fd, step.argv)
            argv = _as_app_user(db_app, inner)
            spawn_env = _clean_root_env()
            pass_fds = (env_fd,)
        else:
            argv = list(step.argv)
            spawn_env = tool_env
            pass_fds = ()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=spawn_env,
            cwd=str(wd) if step.in_app_dir else "/",
            start_new_session=True,
            pass_fds=pass_fds,
        )
    except FileNotFoundError:
        _venvy_log(log_file, f"{step.argv[0]} is not installed on this host — {step.what} can't run.")
        raise RuntimeError(f"{step.argv[0]} is not installed on this host, so {step.what} can't run.")
    finally:
        if env_tf is not None:
            env_tf.close()

    tail = bytearray()
    written = 0

    def _append(data: bytes) -> None:
        with contextlib.suppress(OSError):
            with open(log_file, "ab") as lf:
                lf.write(data)

    async def _drain() -> None:
        nonlocal written
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            if written < _PRESTART_LOG_MAX:
                written += len(chunk)
                await asyncio.to_thread(_append, chunk)
                if written >= _PRESTART_LOG_MAX:
                    await asyncio.to_thread(_append, b"\n[venvy] (more output; the end follows when it finishes)\n")
                continue
            tail.extend(chunk)
            if len(tail) > _PRESTART_TAIL_MAX:
                del tail[:len(tail) - _PRESTART_TAIL_MAX]

    try:
        await asyncio.wait_for(asyncio.gather(_drain(), proc.wait()), timeout=step.timeout)
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
        if isinstance(sys.exc_info()[1], asyncio.TimeoutError):
            _venvy_log(log_file, f"{step.what} timed out after {step.timeout} seconds.")
            raise RuntimeError(f"{step.what} timed out after {step.timeout} seconds.")
        raise
    text = tail.decode(errors="replace").strip()
    if text:                                     # only past _PRESTART_LOG_MAX
        _append((text[-6000:] + "\n").encode())
    elif written:
        _append(b"\n")                          # the [venvy] line below starts on a line of its own
    if proc.returncode != 0:
        _venvy_log(log_file, f"{step.what} failed (exit code {proc.returncode}).")
        raise RuntimeError(f"{step.what} failed — see the Logs tab.")
    _venvy_log(log_file, f"{step.what} finished.")


def _mark_start_error(session: Session, db_app: App, msg: str, log_file: Optional[Path] = None) -> None:
    if log_file is not None:
        _venvy_log(log_file, msg)
    db_app.status = "error"
    session.add(db_app)
    session.commit()
    raise RuntimeError(msg)


def _docker_env_file(app_id: int, env: Dict[str, str]) -> Path:
    """docker --env-file: one NAME=value per line, in a folder only Venvy can read."""
    d = DOCKER_ENV_DIR
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        d.chmod(0o700)
    path = d / f"{app_id}.env"
    lines = []
    for k, v in env.items():
        if "\n" in v or "\r" in v:
            raise RuntimeError(f"Variable {k} has a line break in it, which Docker can't pass to a container.")
        lines.append(f"{k}={v}")
    tmp = d / f".{app_id}.env.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    return path


_DOCKER_SKIP_ENV = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "VIRTUAL_ENV", "PWD", "OLDPWD", "TERM",
                    "XDG_RUNTIME_DIR", "LANG", "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID"}


async def _docker_cleanup(app) -> None:
    """Remove what a docker / compose app may have left (a container killed
    with its CLI, a compose project)."""
    env = _clean_root_env()
    name = runtimes.docker_name(app.id)
    if app.runtime == "docker":
        argv = ["docker", "rm", "-f", name]
    elif app.runtime == "compose":
        # Down by project name (or with the checked copy): the app's own file is
        # never handed to the daemon unchecked.
        from venvy import compose_guard
        f = compose_guard.rendered_file(name)
        argv = ["docker", "compose", "-p", name] + (["-f", str(f)] if f else []) + ["down", "--remove-orphans"]
    else:
        return
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                    env=env)
        await asyncio.wait_for(proc.wait(), timeout=120)


async def _launch_port_free(spec, app_id: int, port: int) -> bool:
    """Whether *port* is free for the app about to start. A docker / compose
    app's own containers from before may hold it: Docker restarts them by
    itself when the host boots (a `restart:` policy), before Venvy starts the
    app. They are removed before the launch anyway; here that happens first,
    or the app would be moved off its port, and off the tunnel route and
    domain that point at it."""
    if await asyncio.to_thread(_verify_port_free, port):
        return True
    if spec.runtime not in ("docker", "compose"):
        return False
    await _docker_cleanup(types.SimpleNamespace(**{**vars_of(spec), "id": app_id}))
    return await asyncio.to_thread(_verify_port_free, port)


def _signal_of(app) -> int:
    name = (getattr(app, "stop_signal", None) or "SIGTERM").upper()
    return int(getattr(signal, name, signal.SIGTERM))


def _grace_of(app) -> float:
    t = getattr(app, "stop_timeout", None)
    return float(t) if t else _STOP_GRACE_SEC


async def _write_log_lines(log_file: Path, lines: List[bytes], when=None) -> None:
    """Append lines to an app's log with a timestamp, rotating when it's too big."""
    ts = (when or utc_now()).strftime("%Y-%m-%d %H:%M:%S").encode()
    data = b"".join(b"[" + ts + b"] " + ln[:_LOG_MAX_LINE] + b"\n" for ln in lines)

    def _w() -> None:
        with open(log_file, "ab") as f:
            f.write(data)
        try:
            too_big = os.stat(log_file).st_size > _LOG_MAX_BYTES
        except OSError:
            too_big = False
        if too_big:
            _rotate_log(log_file)
    try:
        await asyncio.to_thread(_w)
    except Exception as exc:
        logging.warning(f"Writing app log failed: {exc}")
    from venvy import logship
    logship.offer(log_file, lines, when)


def _service_paths(spec, wd: Path) -> tuple:
    """(read-write, read-only) folders a sandboxed service gets to see."""
    from venvy import appspec, launcher, toolchains, volumes
    from venvy.config import ASSETS_DIR, DATA_DIR
    rw = [str(Path(spec.app_dir)), str(appspec.state_root(spec)), str(launcher.ENV_DIR / str(spec.id))]
    ro: List[str] = []
    with contextlib.suppress(Exception):
        for path, read_only in volumes.mounts(appspec.owner_id(spec)):
            (ro if read_only else rw).append(str(path))
    for p in (ASSETS_DIR, toolchains.TOOLCHAINS_DIR):
        with contextlib.suppress(OSError):
            if DATA_DIR.resolve() in Path(p).resolve().parents:
                ro.append(str(p))
    if appspec.SHARED_CACHE:
        rw.append(str(appspec.cache_dir(spec)))
    return [p for p in rw if Path(p).exists()], [p for p in ro if Path(p).exists()]


async def _start_app_process(app_id: int, session: Session) -> App:
    """Start the app. Caller holds _app_lock(app_id)."""
    from venvy import appspec, groups, launcher, secrets_ext, toolchains
    # populate_existing: the caller's session may hold a copy loaded before
    # we got the lock.
    db_app = session.get(App, app_id, populate_existing=True)
    if not db_app:
        raise ValueError("App not found.")
    # For log lines: the stored row's id, not the caller's (request-supplied) value.
    log_id = int(db_app.id or 0)

    if db_app.pid and await asyncio.to_thread(_app_pid_alive, db_app):
        db_app.status = "running"
        session.add(db_app)
        session.commit()
        logging.info(f"start_app: '{db_app.name}' already running (PID {db_app.pid})")
        return db_app

    # Its own session for the parent's row: a query on ours would flush the
    # change below and hold SQLite's write lock across every await of the start.
    spec = groups.effective(db_app, None)
    db_app.pid = None
    db_app.pid_start = None
    if spec.runtime not in runtimes.RUNTIMES:
        _mark_start_error(session, db_app, f"Unknown runtime '{spec.runtime}'.")

    if runtimes.entry_missing(spec.runtime, spec.entrypoint, spec.start_command):
        _mark_start_error(session, db_app, "No entrypoint configured. Set one (or a start command) in the app's Settings tab first.")

    wd = appspec.work_dir(spec)
    ids = await asyncio.to_thread(_ensure_app_user, spec)
    log_file = _app_log_file(db_app)
    if ids:
        # Venvy-only: apps must not read each other's logs.
        APP_LOGS_DIR.mkdir(mode=0o700, exist_ok=True)
        APP_LOGS_DIR.chmod(0o700)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if log_file.exists() and log_file.stat().st_size > min(5 * 1024 * 1024, _LOG_MAX_BYTES):
        try:
            await asyncio.to_thread(_rotate_log, log_file)
        except Exception as exc:
            logging.warning(f"Log rotation failed: {exc}")

    if not await asyncio.to_thread(log_file.exists):
        await asyncio.to_thread(log_file.touch)
    from venvy import logship
    logship.register(log_file, db_app.id, db_app.name, db_app.process_name or "web")

    problem = appspec.work_dir_problem(spec)
    if problem:
        _mark_start_error(session, db_app, problem, log_file)

    # The language version the app asks for, installed on first use.
    try:
        await toolchains.ensure_for_app(spec, wd, lambda m: _venvy_log(log_file, m))
    except Exception as exc:
        _mark_start_error(session, db_app, f"Could not install the toolchain: {exc}", log_file)

    extra_path = toolchains.path_dirs(spec, wd)
    problem = (None if spec.start_command and spec.runtime not in ("docker", "compose")
               else runtimes.tools_error(spec.runtime, extra_path=extra_path)) or runtimes.start_problem(
        spec.runtime, wd, spec.entrypoint)
    if problem and not (spec.start_command and spec.runtime not in ("docker", "compose")
                        and "does not exist" not in problem and "no go.mod" not in problem.lower()):
        _mark_start_error(session, db_app, problem, log_file)

    if spec.runtime == "python":
        venv_python = Path(spec.venv_path) / "bin" / "python"
        if not venv_python.exists():
            _mark_start_error(session, db_app, "Virtual environment python not found.", log_file)

    if not _try_decrypt_secret(db_app.env_json)[0]:
        # Starting it anyway would run the app without its configuration —
        # against a default database, with no API keys — and look healthy.
        msg = ("This app's environment variables can't be decrypted (the .venvy_key file is missing "
               "or was replaced). Re-enter them in Settings, or restore the key, then start again.")
        _mark_start_error(session, db_app, msg, log_file)

    if db_app.process_kind not in ("worker", "tunnel") and not db_app.port and (
            runtimes.needs_port(spec.runtime, spec.entrypoint) or db_app.process_kind == "replica" or _AUTO_PORT):
        # The command line names the port (a static site, php -S, docker -p),
        # or a replica that needs one of its own: there has to be one before
        # the app starts rather than one found afterwards.
        db_app.port = await asyncio.to_thread(find_free_port)
        if not db_app.port:
            _mark_start_error(session, db_app, "No free port is available for this app.", log_file)
        session.add(db_app)
        session.commit()

    try:
        extra_env = await secrets_ext.resolve(spec, log=lambda m: _venvy_log(log_file, m))
    except Exception as exc:
        _mark_start_error(session, db_app, f"Could not load the app's secrets: {exc}", log_file)
    env = _build_app_env(db_app, spec, extra_env)
    if ids:
        env.update(HOME=str(appspec.home_dir(spec)), USER=_app_username(appspec.owner_id(spec)),
                   LOGNAME=_app_username(appspec.owner_id(spec)))
    _cancel_log_task(app_id)

    process: Optional[asyncio.subprocess.Process] = None
    try:
        await asyncio.to_thread(secrets_ext.write_files, spec, ids)
        for step in runtimes.first_run_steps(spec.runtime, wd, spec.entrypoint, spec):
            await _run_prestart_step(spec, step, log_file)

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
            port_free = await _launch_port_free(spec, db_app.id, effective_port)
            if not port_free:
                new_port = await asyncio.to_thread(find_free_port)
                if new_port:
                    logging.warning(
                        f"Port {effective_port} for '{db_app.name}' was taken at launch time; "
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
                    if spec.runtime != "docker":
                        env["PORT"] = str(new_port)
                    if env.get("SERVER_PORT") == str(effective_port):
                        env["SERVER_PORT"] = str(new_port)      # Java apps

        host = "0.0.0.0" if getattr(spec, "bind_public", True) else "127.0.0.1"
        container = spec.runtime in ("docker", "compose")
        try:
            if spec.start_command and not container:
                cmd = appspec.command_argv(spec.start_command, env, wd)
            else:
                cmd = runtimes.start_command(spec.runtime, wd, spec.entrypoint, db_app.port, spec.venv_path,
                                             host=host, app=types.SimpleNamespace(**{**vars_of(spec), "id": db_app.id}))
        except ValueError as exc:
            _mark_start_error(session, db_app, str(exc), log_file)
        if container:
            denv = {k: v for k, v in env.items() if k not in _DOCKER_SKIP_ENV}
            if spec.runtime == "docker":
                env_file = await asyncio.to_thread(_docker_env_file, db_app.id, denv)
                start_args = appspec.command_argv(spec.start_command, env) if spec.start_command else []
                out = []
                for w in cmd:
                    if w == "{ENV_FILE}":
                        out.append(str(env_file))
                    elif w == "{START_ARGS}":
                        out.extend(start_args)
                    else:
                        out.append(w)
                cmd = out
            else:
                # Its variables are put into the file when it is rendered; the
                # daemon gets that checked copy, never the app's own file.
                from venvy import compose_guard
                try:
                    cmd = await asyncio.to_thread(compose_guard.pinned_argv,
                                                  types.SimpleNamespace(**{**vars_of(spec), "id": db_app.id}), cmd, denv)
                except compose_guard.ComposeRefused as exc:
                    _mark_start_error(session, db_app, str(exc), log_file)
            await _docker_cleanup(types.SimpleNamespace(**{**vars_of(spec), "id": db_app.id}))
        elif spec.runtime == "command" and not spec.start_command and spec.entrypoint:
            exe = wd / spec.entrypoint
            if exe.is_file() and not os.access(exe, os.X_OK):
                await _run_prestart_step(spec, runtimes.Step(["chmod", "u+x", str(exe)], 30, "chmod +x"), log_file,
                                         first_run=False)

        unit_name = f"venvy-app-{app_id}"
        used_systemd_scope = False
        isolated = _isolation_active()

        if await asyncio.to_thread(launcher.available):
            env_path = None
            if not container:
                # First, so its folder exists and is mounted writable: exec_env.py deletes it once read.
                env_path = await asyncio.to_thread(launcher.write_env_file, db_app.id, env, ids)
            rw_paths, ro_paths = _service_paths(types.SimpleNamespace(**{**vars_of(spec), "id": db_app.id}), wd)
            props = launcher.properties(spec, ids=ids, wd=wd, signal_name=spec.stop_signal or "SIGTERM",
                                        grace=_grace_of(spec), rw_paths=rw_paths, ro_paths=ro_paths, as_root=container)
            pid = await launcher.start(types.SimpleNamespace(id=db_app.id), cmd, props, env_path)
            db_app.systemd_scope = launcher.unit_name(db_app.id)
            db_app.last_start_time = utc_now()
            db_app.last_exit_code = None
            if not pid:
                code = await launcher.wait_exit_code(db_app.id)
                db_app.status = "stopped"
                db_app.pid = None
                db_app.pid_start = None
                db_app.systemd_scope = None
                db_app.last_exit_code = code if code is not None else 1
                db_app.last_stop_time = utc_now()
                session.add(db_app)
                session.commit()
                launcher.follow(db_app.id, log_file, lambda lines, when: _write_log_lines(log_file, lines, when))
                await launcher.unfollow(db_app.id)
                logging.warning(f"App '{db_app.name}' exited right after it started (code {db_app.last_exit_code}).")
                return db_app
            db_app.pid = pid
            db_app.pid_start = await asyncio.to_thread(_proc_start_id, pid)
            db_app.status = "running"
            session.add(db_app)
            session.commit()
            launcher.follow(db_app.id, log_file, lambda lines, when: _write_log_lines(log_file, lines, when))
            _routes_changed()
            logging.info(f"App '{db_app.name}' started (PID {pid}) as {db_app.systemd_scope}")
            return db_app

        env_tf = None
        try:
            if isolated and not container:
                # Never pass untrusted app environment variables to root-privileged
                # wrappers (systemd-run / setpriv). Apply env after dropping uid/gid
                # + setting --no-new-privs via exec_env.py reading an unlinked fd.
                env_tf, env_fd = _prepare_exec_env(env)
                inner_cmd = _as_app_user(spec, _exec_env_argv(env_fd, cmd))
                spawn_env = _clean_root_env()
                pass_fds = (env_fd,)
            elif container:
                inner_cmd = list(cmd)
                spawn_env = _clean_root_env() if isolated else {**child_base_env(), "PATH": runtimes.tool_search_path()}
                pass_fds = ()
            else:
                inner_cmd = list(cmd)
                spawn_env = env
                pass_fds = ()

            launch_cmd = inner_cmd
            if await asyncio.to_thread(_systemd_run_available):
                await _clear_stale_scope(unit_name)
                scope_cmd = [_resolve_system_bin("systemd-run"), "--scope", "--quiet", f"--unit={unit_name}"]
                if spec.memory_limit_mb:
                    scope_cmd += ["-p", f"MemoryMax={spec.memory_limit_mb}M"]
                if spec.cpu_limit_percent:
                    scope_cmd += ["-p", f"CPUQuota={spec.cpu_limit_percent}%"]
                if spec.pids_limit:
                    scope_cmd += ["-p", f"TasksMax={spec.pids_limit}"]
                if spec.io_weight:
                    scope_cmd += ["-p", f"IOWeight={spec.io_weight}"]
                scope_cmd += ["--"] + inner_cmd
                launch_cmd = scope_cmd
                used_systemd_scope = True

            if spec.nofile_limit and not used_systemd_scope:
                launch_cmd = ["prlimit", f"--nofile={spec.nofile_limit}", "--"] + launch_cmd if shutil.which("prlimit") else launch_cmd
            process = await asyncio.create_subprocess_exec(
                *launch_cmd,
                cwd=str(wd),
                # Never Venvy's own stdin: started from a shell, an app that reads
                # input would otherwise compete with it for the terminal.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=spawn_env,
                start_new_session=True,
                pass_fds=pass_fds,
            )
        finally:
            if env_tf is not None:
                env_tf.close()

        if process.stdout:
            task = asyncio.create_task(log_streamer(process.stdout, log_file))
            _log_tasks[app_id] = task

        pid_start = await asyncio.to_thread(_proc_start_id, process.pid)
        db_app.pid = process.pid
        db_app.pid_start = pid_start
        _active_app_pids[app_id] = process.pid
        db_app.status = "running"
        db_app.last_start_time = utc_now()
        db_app.last_exit_code = None
        db_app.systemd_scope = unit_name if used_systemd_scope else None
        session.add(db_app)
        session.commit()
        _routes_changed()
        logging.info(
            f"App '{db_app.name}' started (PID {process.pid})"
            + (f" under systemd scope '{unit_name}.scope'" if used_systemd_scope else " (no resource isolation)")
        )

        async def _reap_process(proc: asyncio.subprocess.Process, _app_id: int, _start_id: Optional[str]):
            try:
                rc = await proc.wait()
                logging.info("App %d (PID %d) exited with code %s", int(_app_id), proc.pid, rc)

                def _record_exit() -> None:
                    # In a thread: a write that has to wait for SQLite's lock must
                    # never hold up the event loop (and every request with it).
                    with Session(engine) as s:
                        app_to_reap = s.get(App, _app_id)
                        same_proc = (
                            app_to_reap is not None
                            and app_to_reap.pid == proc.pid
                            and (not _start_id or not app_to_reap.pid_start or app_to_reap.pid_start == _start_id)
                        )
                        if same_proc:
                            crashed = (app_to_reap.status == "running" and rc not in (0, None)
                                       and _app_id not in _stopping_on_purpose)
                            if crashed:
                                notify.emit("app_crashed", f"{app_to_reap.name} died unexpectedly",
                                            f"It exited with code {rc} (restart policy: {app_to_reap.restart_policy}).",
                                            app_to_reap.id, app_to_reap.name, "error")
                            app_to_reap.status = "stopped"
                            app_to_reap.pid = None
                            app_to_reap.pid_start = None
                            app_to_reap.systemd_scope = None
                            app_to_reap.last_exit_code = rc
                            app_to_reap.last_stop_time = utc_now()
                            s.add(app_to_reap)
                            s.commit()
                            _routes_changed()
                try:
                    await asyncio.to_thread(_record_exit)
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

        reap_task = asyncio.create_task(_reap_process(process, db_app.id, pid_start))
        _running_tasks.add(reap_task)
        process = None  # ownership transferred to _reap_process
        return db_app
    except BaseException as exc:
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await process.wait()
        if process is not None and _active_app_pids.get(app_id) == process.pid:
            _active_app_pids.pop(app_id, None)
        _cancel_log_task(app_id)
        if isinstance(exc, Exception):
            reason = str(exc).replace("\r", " ").replace("\n", " ")
            logging.error("Failed to start app %d: %s", log_id, reason)
        with contextlib.suppress(Exception):
            session.rollback()
            fresh = session.get(App, app_id, populate_existing=True)
            if fresh is not None:
                fresh.status = "error"
                fresh.pid = None
                fresh.pid_start = None
                fresh.systemd_scope = None
                session.add(fresh)
                session.commit()
        raise


# How long an app gets to shut down after SIGTERM before it is killed. A web
# server finishing its requests, or anything flushing to disk, needs more than
# an instant; an app that exits at once is not held up by a longer allowance.
_STOP_GRACE_SEC = max(0.5, float(os.getenv("VENVY_STOP_GRACE_SEC", "5")))


async def _kill_pid_tree(pid: int, first_signal: int = signal.SIGTERM, grace: Optional[float] = None) -> None:
    """Kill a process and its entire descendant tree, async-safely without self-killing.
    *first_signal* (the app's stop signal) first, SIGKILL after *grace* seconds."""
    def _get_pids_to_kill(target_pid: int) -> List[int]:
        res = [target_pid]
        if psutil:
            try:
                res += [c.pid for c in psutil.Process(target_pid).children(recursive=True)]
            except Exception:
                pass
        else:
            try:
                parent_to_children: Dict[int, List[int]] = {}
                for p in Path("/proc").iterdir():
                    if p.name.isdigit():
                        try:
                            rest = (p / "stat").read_text().rsplit(")", 1)[1].split()
                            parent_to_children.setdefault(int(rest[1]), []).append(int(p.name))
                        except Exception:
                            pass
                queue = [target_pid]
                visited = {target_pid}
                while queue:
                    cur = queue.pop(0)
                    for child in parent_to_children.get(cur, []):
                        if child not in visited:
                            visited.add(child)
                            res.append(child)
                            queue.append(child)
            except Exception:
                pass
        return list(set(res))

    def _pid_still_same(p: int, start_id: Optional[str]) -> bool:
        if not _pid_alive(p):
            return False
        if start_id:
            return _proc_start_id(p) == start_id
        return True

    async def _wait_all_dead(targets: List[tuple], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            alive = await asyncio.to_thread(lambda: any(_pid_still_same(p, sid) for p, sid in targets))
            if not alive:
                return True
            await asyncio.sleep(0.05)
        return not await asyncio.to_thread(lambda: any(_pid_still_same(p, sid) for p, sid in targets))

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

    _SIGTERM_GRACE = grace if grace is not None else _STOP_GRACE_SEC
    _SIGKILL_WAIT  = 0.5

    # Snapshot the full descendant tree with start-time fingerprints so a PID
    # that exits and gets reused during the grace period is never killed.
    def _snapshot_tree():
        return [(p, _proc_start_id(p)) for p in _get_pids_to_kill(pid)]

    targets = await asyncio.to_thread(_snapshot_tree)

    try:
        pgid = os.getpgid(pid)
        # CRITICAL BUGFIX: Ensure we never call killpg on Venvy's own PGID
        if pgid > 0 and pgid != os.getpgid(0):
            await _sig_pgid(pgid, first_signal)
            if await _wait_all_dead(targets, _SIGTERM_GRACE):
                return
            await _sig_pgid(pgid, signal.SIGKILL)
            if await _wait_all_dead(targets, _SIGKILL_WAIT):
                return
    except Exception:
        pass

    # Fallback (and cleanup for descendants that called setsid into their own PGID)
    survivors = await asyncio.to_thread(lambda: [(p, sid) for p, sid in targets if _pid_still_same(p, sid)])
    if not survivors:
        return
    for p, sid in survivors:
        if await asyncio.to_thread(_pid_still_same, p, sid):
            await _sig_pid(p, signal.SIGTERM)

    if await _wait_all_dead(survivors, _SIGTERM_GRACE):
        return

    for p, sid in survivors:
        if await asyncio.to_thread(_pid_still_same, p, sid):
            await _sig_pid(p, signal.SIGKILL)
    await _wait_all_dead(survivors, _SIGKILL_WAIT)


async def stop_app_process_logic(app_id: int, session: Session, manual: bool = False) -> None:
    async with _app_lock(app_id):
        await _stop_app_process(app_id, session, manual)


# Apps Venvy is stopping on purpose right now: their exit is not a crash.
_stopping_on_purpose: Set[int] = set()


async def _stop_app_process(app_id: int, session: Session, manual: bool = False) -> None:
    """Stop the app. Caller holds _app_lock(app_id)."""
    _stopping_on_purpose.add(app_id)
    try:
        await _stop_app_process_inner(app_id, session, manual)
    finally:
        _stopping_on_purpose.discard(app_id)


async def _stop_app_process_inner(app_id: int, session: Session, manual: bool = False) -> None:
    db_app = session.get(App, app_id, populate_existing=True)
    if not db_app:
        _cancel_log_task(app_id)
        return
    from venvy import groups, launcher
    # Read before anything is changed on the row: a query after a change would
    # flush it, and the write transaction that starts would be held across the
    # awaits below — every other write (the reaper's, on the event loop) would
    # then wait on SQLite's lock with the whole server blocked.
    spec = groups.effective(db_app, None)
    # Recorded even when nothing is running: that is exactly the state of a
    # crash-looping app between two automatic restarts, and Stop has to end
    # the loop there too.
    if manual:
        db_app.last_manually_stopped_at = utc_now()
        _restart_backoff.pop(app_id, None)
        session.add(db_app)
        session.commit()
    service = bool(db_app.systemd_scope and db_app.systemd_scope.endswith(".service"))
    if not db_app.pid:
        _cancel_log_task(app_id)
        if service:
            await launcher.stop(app_id, _grace_of(spec))
            await launcher.unfollow(app_id)
        if spec.runtime in ("docker", "compose"):
            await _docker_cleanup(types.SimpleNamespace(**{**vars_of(spec), "id": app_id}))
        db_app.status = "stopped"
        db_app.systemd_scope = None
        session.add(db_app)
        session.commit()
        return

    pid = db_app.pid
    if service:
        logging.info(f"Stopping app '{db_app.name}' ({db_app.systemd_scope})")
        await launcher.stop(app_id, _grace_of(spec))
        await launcher.unfollow(app_id)
        if spec.runtime in ("docker", "compose"):
            await _docker_cleanup(types.SimpleNamespace(**{**vars_of(spec), "id": app_id}))
        db_app.status = "stopped"
        db_app.pid = None
        db_app.pid_start = None
        db_app.systemd_scope = None
        db_app.last_stop_time = utc_now()
        db_app.last_exit_code = -15
        session.add(db_app)
        session.commit()
        _routes_changed()
        return
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
        # A scope's stop is SIGTERM; an app that wants another signal gets it first.
        if _signal_of(spec) != signal.SIGTERM:
            await _run_systemctl("kill", f"--signal={_signal_of(spec)}", f"{db_app.systemd_scope}.scope", timeout=5)
        else:
            await _run_systemctl("stop", f"{db_app.systemd_scope}.scope", timeout=_grace_of(spec) + 10)

    # Always also run the signal-based teardown as a fallback/complement if
    # our process is still alive (re-checking pid_start avoids a PID-reuse race).
    try:
        if await asyncio.to_thread(_app_pid_alive, db_app):
            await _kill_pid_tree(pid, _signal_of(spec), _grace_of(spec))
    except Exception as exc:
        logging.warning(f"Error killing PID {pid}: {exc}")
    if spec.runtime in ("docker", "compose"):
        await _docker_cleanup(types.SimpleNamespace(**{**vars_of(spec), "id": app_id}))
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
    _routes_changed()


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
    if failures == 3:
        name = None
        with contextlib.suppress(Exception):
            with Session(engine) as s:
                row = s.get(App, app_id)
                name = row.name if row else None
        notify.emit("app_crash_loop", f"{name or f'App {app_id}'} keeps crashing",
                    f"It died {failures + 1} times in a row; restarts now back off ({delay} s and growing).",
                    app_id, name, "error")
    return True


async def _health_restart(app_id: int) -> None:
    """Restart an app whose health check keeps failing."""
    try:
        await restart_app_process_logic(app_id)
    except Exception as exc:
        logging.error(f"Restart after failed health checks failed for app {app_id}: {exc}")


# Set while a restore is running (from the request until the process re-execs,
# or until the restore fails). Starting apps is held off for the duration.
_restore_in_progress = False


def restore_in_progress() -> bool:
    return _restore_in_progress


def set_restore_in_progress(value: bool) -> None:
    global _restore_in_progress
    _restore_in_progress = value


# Throttle port detection for steady-state running apps (every 5s, immediate during first 15s).
_last_port_scan: Dict[int, float] = {}


async def supervisor_task() -> None:
    """Background loop: detect crashed apps, restart per policy, auto-detect ports."""
    global _supervisor_event
    _supervisor_event = asyncio.Event()

    _last_session_cleanup  = 0.0
    _last_login_cleanup    = 0.0

    def _purge_expired_sessions() -> int:
        with Session(engine) as _cs:
            expired = _cs.exec(
                select(UserSession).where(UserSession.expires_at < utc_now())
            ).all()
            for s in expired:
                _cs.delete(s)
            if expired:
                _cs.commit()
            return len(expired)

    def _fetch_all_app_ids() -> List[int]:
        with Session(engine) as session:
            return [a.id for a in session.exec(select(App)).all() if a.id is not None]

    def _fetch_app_and_alive(aid: int):
        with Session(engine) as session:
            row = session.get(App, aid)
            if row is None:
                return None, False
            return row, _app_pid_alive(row)

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
                    n = await asyncio.to_thread(_purge_expired_sessions)
                    if n:
                        logging.info(f"Purged {n} expired session(s)")
                except Exception as _exc:
                    logging.warning(f"Session cleanup error: {_exc}")

            if now_mono - _last_login_cleanup > 120:
                _last_login_cleanup = now_mono
                _purge_login_attempts()
                _purge_mutation_hits()

            await monitor.health_tick(_health_restart)

            try:
                app_ids = await asyncio.to_thread(_fetch_all_app_ids)
            except Exception as e:
                logging.error(f"Failed to fetch apps: {e}")
                await asyncio.sleep(5)
                continue

            for aid in app_ids:
                try:
                    app_row, is_alive = await asyncio.to_thread(_fetch_app_and_alive, aid)
                except Exception as e:
                    logging.error(f"Failed to fetch app {aid}: {e}")
                    continue
                if app_row is None:
                    continue

                if (app_row.status == "running" and not is_alive
                        and app_row.pid and _active_app_pids.get(app_row.id) == app_row.pid):
                    # Our own child that has just exited: a zombie until its
                    # reaper (_reap_process) collects it, which records the real
                    # exit code and wakes us.
                    continue

                if app_row.status == "running" and not is_alive:
                    died = False
                    async with _app_lock(app_row.id):
                        def _mark_dead(target_id: int, expected_pid: Optional[int], was_service: bool) -> bool:
                            from venvy import launcher
                            # Before the database is opened: ExecStopPost may still be writing it.
                            service_code = launcher.exit_code_wait(target_id) if was_service else None
                            with Session(engine) as update_session:
                                refreshed = update_session.get(App, target_id)
                                if (refreshed and refreshed.status == "running"
                                        and refreshed.pid == expected_pid):
                                    service = bool(refreshed.systemd_scope and refreshed.systemd_scope.endswith(".service"))
                                    refreshed.pid = None
                                    refreshed.pid_start = None
                                    refreshed.systemd_scope = None
                                    refreshed.status = "stopped"
                                    refreshed.last_stop_time = utc_now()
                                    if service:
                                        code = service_code if service_code is not None else launcher.exit_code(target_id)
                                        refreshed.last_exit_code = 1 if code is None else code
                                    elif refreshed.last_exit_code is None:
                                        refreshed.last_exit_code = 1
                                    update_session.add(refreshed)
                                    update_session.commit()
                                    _routes_changed()
                                    return True
                            return False
                        try:
                            died = await asyncio.to_thread(
                                _mark_dead, app_row.id, app_row.pid,
                                bool(app_row.systemd_scope and app_row.systemd_scope.endswith(".service")))
                        except Exception as db_exc:
                            logging.error(f"Failed to update state for {app_row.name}: {db_exc}")
                    if not died:
                        continue
                    from venvy import launcher as _launcher
                    await _launcher.unfollow(app_row.id)
                    with Session(engine) as _s:
                        _now = _s.get(App, app_row.id)
                        exit_code = _now.last_exit_code if _now else 1
                    if exit_code in (0, None):
                        logging.info(f"App '{app_row.name}' (PID {app_row.pid}) exited.")
                    else:
                        logging.warning(f"App '{app_row.name}' (PID {app_row.pid}) died unexpectedly.")
                        notify.emit("app_crashed", f"{app_row.name} died unexpectedly",
                                    f"Process {app_row.pid} is gone (exit code {exit_code}, restart policy: {app_row.restart_policy}).",
                                    app_row.id, app_row.name, "error")
                    if app_row.restart_policy == "on-failure" and exit_code == 0:
                        continue

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
                    age_s = (
                        (utc_now() - _make_tz_aware(app_row.last_start_time)).total_seconds()
                        if app_row.last_start_time is not None else 999.0
                    )
                    if age_s > _RESTART_STABLE_SEC:
                        _restart_backoff.pop(app_row.id, None)
                    # Scan every tick during the first 15s after start so newly bound
                    # ports appear promptly, then throttle to every 5s.
                    last_scan = _last_port_scan.get(app_row.id, 0.0)
                    if age_s <= 15.0 or (now_mono - last_scan >= 5.0):
                        _last_port_scan[app_row.id] = now_mono
                        try:
                            detected_ports = await asyncio.to_thread(_detect_ports_sync, app_row.pid)
                            manual_list = []
                            try:
                                manual_list = json.loads(app_row.manual_ports or "[]")
                            except Exception:
                                pass

                            combined = manual_list if manual_list else list(detected_ports)
                            dp_json = json.dumps(combined)

                            if app_row.detected_ports != dp_json:
                                def _save_ports(target_id: int, ports_list: list, ports_json: str) -> None:
                                    with Session(engine) as update_session:
                                        refreshed = update_session.get(App, target_id)
                                        if refreshed:
                                            manual = json.loads(refreshed.manual_ports or "[]") if refreshed.manual_ports else []
                                            if not refreshed.port and ports_list:
                                                refreshed.port = ports_list[0]
                                            elif (not manual and ports_list and refreshed.port not in ports_list
                                                    and refreshed.runtime not in ("docker", "compose")):
                                                # It was given $PORT but listens somewhere else: that is its port.
                                                refreshed.port = ports_list[0]
                                                _routes_changed()
                                            refreshed.detected_ports = ports_json
                                            update_session.add(refreshed)
                                            update_session.commit()
                                await asyncio.to_thread(_save_ports, app_row.id, combined, dp_json)
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
                            except Exception as exc:
                                logging.warning(f"Automatic restart failed for '{app_row.name}': {exc}")

        except Exception as exc:
            logging.error(f"Supervisor loop error: {exc}")


# app_id -> (root pid, CPU seconds its process tree had used, monotonic time of that reading)
_cpu_seen: Dict[int, tuple] = {}


def _tree_cpu_seconds(p: "psutil.Process") -> float:  # type: ignore
    """CPU time a process has used, plus what its finished children used (once
    it has waited for them — which is what moves a child's time into its parent's
    children_user/children_system)."""
    t = p.cpu_times()
    return t.user + t.system + getattr(t, "children_user", 0.0) + getattr(t, "children_system", 0.0)


def _collect_metrics_tick(active_pids: Dict[int, int], proc_cache: Dict[int, "psutil.Process"]):  # type: ignore
    """Blocking psutil work for one metrics-collector tick, run via
    asyncio.to_thread. Returns `(host_cpu, metrics_snapshot)` so shared dicts are
    only mutated on the event loop."""
    host_cpu = psutil.cpu_percent(interval=None)
    live_pids: set = set()
    now = time.monotonic()
    snapshot: Dict[int, Dict[str, Union[float, int]]] = {}

    for app_id, pid in active_pids.items():
        if not pid:
            continue
        try:
            p = proc_cache.get(pid)
            if p is None:
                p = proc_cache[pid] = psutil.Process(pid)
            live_pids.add(pid)
            with p.oneshot():
                total = _tree_cpu_seconds(p)
                mem = p.memory_info().rss

            for child in p.children(recursive=True):
                cpid = child.pid
                try:
                    cached_child = proc_cache.setdefault(cpid, child)
                    live_pids.add(cpid)
                    with cached_child.oneshot():
                        total += _tree_cpu_seconds(cached_child)
                        mem += cached_child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    proc_cache.pop(cpid, None)
                    continue

            prev = _cpu_seen.get(app_id)
            _cpu_seen[app_id] = (pid, total, now)
            cpu = 0.0
            if prev and prev[0] == pid and now > prev[2]:
                # Negative when a child's time left the tree (an orphan reaped by init).
                cpu = max(0.0, total - prev[1]) / (now - prev[2]) * 100
            snapshot[app_id] = {"cpu": round(cpu, 1), "memory": mem}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _cpu_seen.pop(app_id, None)
            proc_cache.pop(pid, None)
        except Exception:
            pass

    for pid in list(proc_cache.keys()):
        if pid not in live_pids:
            proc_cache.pop(pid, None)
    for app_id in [a for a in _cpu_seen if a not in active_pids]:
        _cpu_seen.pop(app_id, None)
    return host_cpu, snapshot


def _service_pids() -> Dict[int, int]:
    """Apps running as their own systemd service: not Venvy's children, so
    not in _active_app_pids (whose entries the liveness check leaves to the
    child reaper). Their recorded PID, while it is still the same process."""
    out: Dict[int, int] = {}
    with Session(engine) as s:
        for row in s.exec(select(App).where(App.status == "running")).all():
            if row.pid and (row.systemd_scope or "").endswith(".service") and _app_pid_alive(row):
                out[row.id] = row.pid
    return out


async def metrics_collector_task():
    """Background task to collect CPU/RAM usage for all active app processes."""
    if psutil is None:
        return
    proc_cache: Dict[int, psutil.Process] = {} # type: ignore
    psutil.cpu_percent(interval=None)

    while True:
        try:
            global _host_cpu_percent
            active_snapshot = {**await asyncio.to_thread(_service_pids), **_active_app_pids}
            _host_cpu_percent, new_metrics = await asyncio.to_thread(
                _collect_metrics_tick, active_snapshot, proc_cache
            )
            for _aid in list(new_metrics):
                if _aid in container_usage:
                    new_metrics[_aid] = container_usage[_aid]
            _app_metrics.clear()
            _app_metrics.update(new_metrics)
            monitor.record_tick(dict(_app_metrics), _host_cpu_percent)
            await monitor.flush_if_due()
        except Exception as exc:
            logging.error(f"Metrics engine error: {exc}")
        await asyncio.sleep(2.5)
