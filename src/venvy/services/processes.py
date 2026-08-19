"""Launching, supervising the exit of, and tearing down managed app processes.

Every app runs in its own process group (``start_new_session=True``) so its
whole tree can be signalled at once, and — when systemd is available — inside a
transient scope that enforces its CPU and memory limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path

import aiofiles
from sqlmodel import Session

from venvy.config import settings
from venvy.core.crypto import decrypt_secret
from venvy.core.net import descendant_pids, find_free_port, is_port_free
from venvy.core.process_compat import (
    get_pgid,
    own_pgid,
    pid_alive,
    pid_alive_async,
    signal_group,
    signal_pid,
    systemd_available,
)
from venvy.core.timeutils import utc_now
from venvy.db.engine import session_scope
from venvy.db.models import App
from venvy.services import registry

__all__ = [
    "AppStartError",
    "build_app_env",
    "kill_process_tree",
    "start_app",
    "stop_app",
]

logger = logging.getLogger(__name__)

#: How long a process tree gets to exit on SIGTERM before SIGKILL follows.
_SIGTERM_GRACE_SECONDS = 1.0
#: How long to confirm death after SIGKILL before giving up and moving on.
_SIGKILL_WAIT_SECONDS = 0.5
#: Poll interval while waiting for a process tree to die.
_DEATH_POLL_INTERVAL = 0.05

#: Environment variables stripped before launching an app. The PYTHON* ones
#: would leak Venvy's own interpreter configuration into the app's venv; the
#: shell ones are arbitrary-code-execution vectors inherited from whatever
#: environment Venvy itself was started in.
_STRIPPED_ENV_KEYS = ("PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV", "PYTHONSTARTUP")
#: Debugger hooks that would attach the app to Venvy's own debug session.
_STRIPPED_ENV_SUBSTRINGS = ("PYDEVD", "DEBUGPY")


class AppStartError(RuntimeError):
    """Raised when an app cannot be started, with an operator-facing message."""


# ── Environment ──────────────────────────────────────────────────────────────


def build_app_env(app: App) -> dict[str, str]:
    """Build the environment for *app*'s process.

    Starts from Venvy's own environment, layers the app's decrypted variables
    on top, then points ``PATH`` and ``VIRTUAL_ENV`` at the app's virtualenv so
    the app sees the same interpreter and tooling a manual ``activate`` would.
    """
    env = os.environ.copy()

    try:
        app_env = json.loads(decrypt_secret(app.env_json) or "{}")
        if isinstance(app_env, dict):
            env.update({str(k): str(v) for k, v in app_env.items()})
    except (ValueError, TypeError):
        logger.warning("App %s has an unparseable env_json; ignoring it.", app.id)

    port = _effective_port(app)
    if port:
        # Both spellings are exported: PORT is the near-universal convention,
        # APP_PORT is what Venvy's own docs and templates use.
        env["APP_PORT"] = str(port)
        env["PORT"] = str(port)

    # Without this a crashing app's traceback can be lost in a full stdio buffer.
    env["PYTHONUNBUFFERED"] = "1"

    venv_bin = str(Path(app.venv_path) / "bin")
    cargo_bin = str(Path.home() / ".cargo" / "bin")
    existing_path = env.get("PATH", "")
    prefix = [venv_bin]
    if cargo_bin not in existing_path:
        prefix.append(cargo_bin)
    env["PATH"] = os.pathsep.join([*prefix, existing_path] if existing_path else prefix)

    for key in _STRIPPED_ENV_KEYS:
        env.pop(key, None)
    for key in [k for k in env if any(marker in k for marker in _STRIPPED_ENV_SUBSTRINGS)]:
        env.pop(key, None)

    env["VIRTUAL_ENV"] = app.venv_path
    return {str(k): str(v) for k, v in env.items()}


def _effective_port(app: App) -> int | None:
    """Return the port the app should bind: a pinned one wins over the assigned one."""
    try:
        manual = json.loads(app.manual_ports or "[]")
        if isinstance(manual, list) and manual:
            return int(manual[0])
    except (ValueError, TypeError):
        pass
    return app.port


# ── Logging ──────────────────────────────────────────────────────────────────


async def _stream_logs(stream: asyncio.StreamReader, log_file: Path) -> None:
    """Append every line from *stream* to *log_file* with a UTC timestamp."""
    try:
        async with aiofiles.open(log_file, "a", encoding="utf-8", errors="replace") as handle:
            while True:
                line = await stream.readline()
                if not line:
                    break
                stamp = utc_now().strftime("%Y-%m-%d %H:%M:%S")
                await handle.write(f"[{stamp}] {line.decode('utf-8', errors='replace')}")
                await handle.flush()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Log streamer failed for %s", log_file)


async def _rotate_log_if_large(log_file: Path, logs_dir: Path) -> None:
    """Rotate *log_file* if it exceeds the configured size, pruning old copies."""

    def _needs_rotation() -> bool:
        return log_file.exists() and log_file.stat().st_size > settings.log_rotate_bytes

    def _old_logs() -> list[Path]:
        return sorted(logs_dir.glob("app.log.*"), key=lambda p: p.stat().st_mtime, reverse=True)

    try:
        # stat() and glob() hit the disk, so they go through a thread rather
        # than stalling the event loop for every app being started.
        if not await asyncio.to_thread(_needs_rotation):
            return
        target = logs_dir / f"app.log.{int(time.time())}"
        await asyncio.to_thread(shutil.move, str(log_file), str(target))
        rotated = await asyncio.to_thread(_old_logs)
        for stale in rotated[settings.log_rotate_keep :]:
            try:
                await asyncio.to_thread(stale.unlink)
            except OSError:
                logger.debug("Could not remove rotated log %s", stale)
    except OSError as exc:
        logger.warning("Log rotation failed for %s: %s", log_file, exc)


# ── Launch ───────────────────────────────────────────────────────────────────


def _build_command(app: App) -> list[str]:
    """Return the argv that starts *app*, per its runtime."""
    if app.runtime == "python":
        return [str(Path(app.venv_path) / "bin" / "python"), app.entrypoint]
    if app.runtime == "rust":
        # cargo resolves the binary from Cargo.toml, so the entrypoint filename
        # is informational for Rust apps.
        return ["cargo", "run"]
    return [app.entrypoint]


def _wrap_in_systemd_scope(command: list[str], app: App, unit_name: str) -> list[str]:
    """Wrap *command* in ``systemd-run --scope`` carrying the app's limits."""
    scope = ["systemd-run", "--scope", "--quiet", f"--unit={unit_name}"]
    if app.memory_limit_mb:
        scope += ["-p", f"MemoryMax={app.memory_limit_mb}M"]
    if app.cpu_limit_percent:
        scope += ["-p", f"CPUQuota={app.cpu_limit_percent}%"]
    return [*scope, "--", *command]


async def _reassign_port_if_taken(app: App, env: dict[str, str]) -> None:
    """Move *app* to a free port if its configured one was claimed meanwhile.

    Mutates *app* and *env* in place; the caller commits.
    """
    if not app.port or await asyncio.to_thread(is_port_free, app.port):
        return

    new_port = await asyncio.to_thread(find_free_port)
    if not new_port:
        logger.warning(
            "Port %s for '%s' is taken and no free port was found; starting anyway.",
            app.port,
            app.name,
        )
        return

    logger.warning(
        "Port %s for '%s' was taken at launch; reassigning to %s.", app.port, app.name, new_port
    )
    old_port = app.port
    app.port = new_port
    try:
        manual = json.loads(app.manual_ports or "[]")
        if isinstance(manual, list) and manual and manual[0] == old_port:
            manual[0] = new_port
            app.manual_ports = json.dumps(manual)
    except (ValueError, TypeError):
        pass
    env["APP_PORT"] = str(new_port)
    env["PORT"] = str(new_port)


def _validate_startable(app: App) -> None:
    """Raise :class:`AppStartError` if *app* is not in a launchable state."""
    if not app.entrypoint and app.runtime != "rust":
        raise AppStartError("No entrypoint configured. Set one in the app's Settings tab first.")
    if app.runtime == "python" and not (Path(app.venv_path) / "bin" / "python").exists():
        raise AppStartError(
            "Virtual environment python not found — the app's venv is missing or broken."
        )


async def start_app(app_id: int, session: Session) -> App:
    """Start *app_id* and record it as running.

    Idempotent: if the recorded PID is still alive the app is simply marked
    running and returned. On failure the app is marked ``error`` and the reason
    is raised.

    :raises AppStartError: if the app cannot be launched.
    :raises ValueError: if *app_id* does not exist.
    """
    app = session.get(App, app_id)
    if not app:
        raise ValueError(f"App {app_id} not found.")

    if app.pid and await pid_alive_async(app.pid):
        app.status = "running"
        session.add(app)
        session.commit()
        logger.info("start_app: '%s' is already running (PID %s)", app.name, app.pid)
        return app

    app.pid = None

    try:
        _validate_startable(app)
    except AppStartError:
        app.status = "error"
        session.add(app)
        session.commit()
        raise

    app_dir = Path(app.app_dir)
    logs_dir = app_dir / "logs"
    await asyncio.to_thread(logs_dir.mkdir, parents=True, exist_ok=True)
    log_file = logs_dir / "app.log"
    await _rotate_log_if_large(log_file, logs_dir)
    if not await asyncio.to_thread(log_file.exists):
        await asyncio.to_thread(log_file.touch)

    env = build_app_env(app)
    registry.cancel_log_task(app_id)

    try:
        await _reassign_port_if_taken(app, env)

        command = _build_command(app)
        unit_name = f"venvy-app-{app_id}"
        use_scope = settings.use_systemd_scopes and await asyncio.to_thread(systemd_available)
        launch_command = _wrap_in_systemd_scope(command, app, unit_name) if use_scope else command

        process = await asyncio.create_subprocess_exec(
            *launch_command,
            cwd=str(app_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            # Its own session and process group, so signalling the app can
            # never reach Venvy itself.
            start_new_session=True,
        )
    except AppStartError:
        app.status = "error"
        session.add(app)
        session.commit()
        raise
    except (OSError, ValueError) as exc:
        logger.error("Failed to start app %s ('%s'): %s", app_id, app.name, exc)
        app.status = "error"
        session.add(app)
        session.commit()
        raise AppStartError(str(exc)) from exc

    if process.stdout is not None:
        registry.set_log_task(app_id, asyncio.create_task(_stream_logs(process.stdout, log_file)))

    app.pid = process.pid
    app.status = "running"
    app.last_start_time = utc_now()
    app.last_exit_code = None
    app.systemd_scope = unit_name if use_scope else None
    registry.register_pid(app_id, process.pid)
    session.add(app)
    session.commit()

    logger.info(
        "App '%s' started (PID %s)%s",
        app.name,
        process.pid,
        f" under systemd scope '{unit_name}.scope'" if use_scope else " (no resource isolation)",
    )

    registry.track_task(asyncio.create_task(_reap_process(process, app_id)))
    return app


async def _reap_process(process: asyncio.subprocess.Process, app_id: int) -> None:
    """Wait for *process* to exit and write its exit status back to the database."""
    try:
        return_code = await process.wait()
        logger.info("App %s (PID %s) exited with code %s", app_id, process.pid, return_code)
        try:
            with session_scope() as session:
                app = session.get(App, app_id)
                # Guard against a race with a restart: only clear the row if it
                # still refers to the process we were waiting on.
                if app and app.pid == process.pid:
                    app.status = "stopped"
                    app.pid = None
                    app.systemd_scope = None
                    app.last_exit_code = return_code
                    app.last_stop_time = utc_now()
                    session.add(app)
                    session.commit()
        except Exception:
            logger.exception("Failed to record exit of app %s in the database", app_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Error while waiting for app %s to exit", app_id)
    finally:
        registry.unregister_pid(app_id)
        registry.trigger_supervisor()


# ── Teardown ─────────────────────────────────────────────────────────────────


async def _wait_all_dead(pids: list[int], timeout: float) -> bool:
    """Poll until every pid in *pids* is gone or *timeout* elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(pid_alive(pid) for pid in pids):
            return True
        await asyncio.sleep(_DEATH_POLL_INTERVAL)
    return not any(pid_alive(pid) for pid in pids)


async def kill_process_tree(pid: int) -> None:
    """Terminate *pid* and every descendant, escalating SIGTERM to SIGKILL.

    The full descendant list is snapshotted first. A wrapper process (a shell
    script, ``npm``, a dev-server launcher) frequently dies on SIGTERM before
    its child does; waiting only on the leader would declare success while a
    grandchild still holds the port.
    """
    pids = await asyncio.to_thread(descendant_pids, pid)

    # Signalling the group is both faster and more complete than walking the
    # tree — but never Venvy's own group, which would be suicide.
    group = get_pgid(pid)
    if group is not None and group > 0 and group != own_pgid():
        signal_group(group, signal.SIGTERM)
        if await _wait_all_dead(pids, _SIGTERM_GRACE_SECONDS):
            return
        signal_group(group, signal.SIGKILL)
        await _wait_all_dead(pids, _SIGKILL_WAIT_SECONDS)
        return

    for target in pids:
        signal_pid(target, signal.SIGTERM)
    if await _wait_all_dead(pids, _SIGTERM_GRACE_SECONDS):
        return

    for target in pids:
        if pid_alive(target):
            signal_pid(target, signal.SIGKILL)
    await _wait_all_dead(pids, _SIGKILL_WAIT_SECONDS)


async def _stop_systemd_scope(scope: str) -> None:
    """Ask systemd to stop the app's transient scope."""
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "stop",
            f"{scope}.scope",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        if process.returncode != 0:
            logger.debug(
                "systemctl stop %s.scope: %s", scope, stderr.decode(errors="replace").strip()
            )
    except (TimeoutError, OSError) as exc:
        logger.warning("systemctl stop failed for %s.scope: %s", scope, exc)


async def stop_app(app_id: int, session: Session, manual: bool = False) -> None:
    """Stop *app_id* and record it as stopped.

    *manual* marks the stop as operator-initiated, which stops the supervisor
    from immediately restarting an app whose policy is ``always``.
    """
    registry.cancel_log_task(app_id)

    app = session.get(App, app_id)
    if not app:
        return

    if manual:
        app.last_manually_stopped_at = utc_now()

    if not app.pid:
        app.status = "stopped"
        session.add(app)
        session.commit()
        return

    pid = app.pid
    logger.info("Stopping app '%s' (PID %s)", app.name, pid)

    if app.systemd_scope:
        await _stop_systemd_scope(app.systemd_scope)

    # Signal-based teardown always runs too. `systemd-run --scope` execs in
    # place, so the scope shares this PID and the second pass is a harmless
    # no-op when systemd already did the work — and the safety net when the
    # scope was never created or systemd could not reap it.
    try:
        await kill_process_tree(pid)
    except Exception as exc:
        logger.warning("Error killing process tree for PID %s: %s", pid, exc)

    app.status = "stopped"
    app.pid = None
    app.systemd_scope = None
    app.last_stop_time = utc_now()
    # Negative exit codes mean "killed by signal N", matching what
    # asyncio.subprocess reports for a process the OS terminated.
    app.last_exit_code = -signal.SIGTERM
    registry.unregister_pid(app_id)
    session.add(app)
    session.commit()
