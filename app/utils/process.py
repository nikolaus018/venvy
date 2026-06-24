import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Dict, List, Optional


import aiofiles
from sqlmodel import Session, select

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None


from app.config import *
from app.database import *
from app.utils.network import *

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


async def log_streamer(stream, log_file: Path) -> None:
    """Read lines from *stream* and append timestamped entries to *log_file*."""
    try:
        async with aiofiles.open(log_file, "a") as f:
            while True:
                line = await stream.readline()
                if not line:
                    break
                ts = utc_now().strftime("%Y-%m-%d %H:%M:%S")
                await f.write(f"[{ts}] {line.decode('utf-8', errors='replace')}")
                await f.flush()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logging.error(f"Log streamer error: {exc}")


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
        return True  # EPERM: process exists, owned by root — alive
    except ProcessLookupError:
        return False  # ESRCH: no such process
    except OSError:
        return False


async def _pid_alive_async(pid: int) -> bool:
    """Non-blocking wrapper — runs _pid_alive in a thread executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _pid_alive, pid)


async def start_app_process_logic(app_id: int, session: Session) -> App:
    db_app = session.get(App, app_id)
    if not db_app:
        raise ValueError("App not found.")

    if db_app.pid and await _pid_alive_async(db_app.pid):
        db_app.status = "running"
        session.add(db_app)
        session.commit()
        logging.info(f"start_app: '{db_app.name}' already running (PID {db_app.pid})")
        return db_app

    db_app.pid = None

    if not db_app.entrypoint and db_app.runtime != "rust":
        db_app.status = "error"
        session.add(db_app)
        session.commit()
        raise RuntimeError(
            "No entrypoint configured. Set one in the app's Settings tab first."
        )

    if db_app.runtime == "python":
        venv_python = Path(db_app.venv_path) / "bin" / "python"
        if not venv_python.exists():
            db_app.status = "error"
            session.add(db_app)
            session.commit()
            raise RuntimeError("Virtual environment python not found.")

    app_dir = Path(db_app.app_dir)
    logs_dir = app_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "app.log"

    if log_file.exists() and log_file.stat().st_size > 5 * 1024 * 1024:
        try:
            target = logs_dir / f"app.log.{int(time.time())}"
            await asyncio.to_thread(shutil.move, str(log_file), str(target))
            rotated = sorted(
                logs_dir.glob("app.log.*"),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            for old_log in rotated[5:]:
                try:
                    await asyncio.to_thread(old_log.unlink)
                except Exception:
                    pass
        except Exception as exc:
            logging.warning(f"Log rotation failed: {exc}")

    if not await asyncio.to_thread(log_file.exists):
        await asyncio.to_thread(log_file.touch)

    env = _build_app_env(db_app)
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

        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(app_dir),
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

        if process.stdout:
            task = asyncio.create_task(log_streamer(process.stdout, log_file))
            _log_tasks[app_id] = task

        db_app.pid = process.pid
        _active_app_pids[app_id] = process.pid
        db_app.status = "running"
        db_app.last_start_time = utc_now()
        db_app.last_exit_code = None
        session.add(db_app)
        session.commit()
        logging.info(f"App '{db_app.name}' started (PID {process.pid})")

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
                            app_to_reap.last_exit_code = rc
                            app_to_reap.last_stop_time = utc_now()
                            s.add(app_to_reap)
                            s.commit()
                except Exception as db_exc:
                    logging.error(
                        f"Failed to update DB for reaped App {_app_id}: {db_exc}"
                    )
            except Exception as exc:
                logging.error(f"Error while waiting for App {_app_id}: {exc}")
            finally:
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
                res += [
                    c.pid for c in psutil.Process(target_pid).children(recursive=True)
                ]
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

    async def _wait_dead(p: int, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not _pid_alive(p):
                return True
            await asyncio.sleep(0.05)
        return not _pid_alive(p)

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
    _SIGKILL_WAIT = 0.5

    try:
        pgid = os.getpgid(pid)
        # CRITICAL BUGFIX: Ensure we never call killpg on Venvy's own PGID
        if pgid > 0 and pgid != os.getpgid(0):
            await _sig_pgid(pgid, signal.SIGTERM)
            if await _wait_dead(pid, _SIGTERM_GRACE):
                return
            await _sig_pgid(pgid, signal.SIGKILL)
            await _wait_dead(pid, _SIGKILL_WAIT)
            return
    except Exception:
        pass

    # Fallback to individual process teardown if session/PGID check failed
    pids = await asyncio.to_thread(_get_pids_to_kill, pid)
    for p in pids:
        await _sig_pid(p, signal.SIGTERM)

    if await _wait_dead(pid, _SIGTERM_GRACE):
        return

    for p in pids:
        if _pid_alive(p):
            await _sig_pid(p, signal.SIGKILL)
    await _wait_dead(pid, _SIGKILL_WAIT)


async def stop_app_process_logic(
    app_id: int, session: Session, manual: bool = False
) -> None:
    _cancel_log_task(app_id)

    db_app = session.get(App, app_id)
    if not db_app:
        return
    if not db_app.pid:
        db_app.status = "stopped"
        session.add(db_app)
        session.commit()
        return

    if manual:
        db_app.last_manually_stopped_at = utc_now()

    pid = db_app.pid
    logging.info(f"Stopping app '{db_app.name}' (PID {pid})")

    try:
        await _kill_pid_tree(pid)
    except Exception as exc:
        logging.warning(f"Error killing PID {pid}: {exc}")

    db_app.status = "stopped"
    db_app.pid = None
    db_app.last_stop_time = utc_now()
    db_app.last_exit_code = -15

    session.add(db_app)
    session.commit()


async def supervisor_task() -> None:
    """Background loop: detect crashed apps, restart per policy, auto-detect ports."""
    global _supervisor_event
    _supervisor_event = asyncio.Event()

    _last_session_cleanup = 0.0
    _last_login_cleanup = 0.0

    while True:
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_supervisor_event.wait(), timeout=1.0)
            _supervisor_event.clear()

            now_mono = time.monotonic()

            if now_mono - _last_session_cleanup > 600:
                _last_session_cleanup = now_mono
                try:
                    with Session(engine) as _cs:
                        expired = _cs.exec(
                            select(UserSession).where(
                                UserSession.expires_at < utc_now()
                            )
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
                # Import here to avoid circular dependency at module load time.
                # The auth router is guaranteed to be imported by the time the
                # supervisor runs (it's started in the lifespan after all routers
                # are registered).
                try:
                    from app.routers.auth import _login_attempts  # noqa: PLC0415
                    expired_keys = [
                        k
                        for k, v in list(_login_attempts.items())
                        if not v.get("locked_until")
                        or time.time() > v["locked_until"] + 300
                    ]
                    for k in expired_keys:
                        _login_attempts.pop(k, None)
                except Exception as _imp_exc:
                    logging.debug(f"Login cleanup skipped: {_imp_exc}")

            apps_to_check: List[App] = []
            try:
                with Session(engine) as session:
                    apps_to_check = session.exec(select(App)).all()
            except Exception as e:
                logging.error(f"Failed to fetch apps: {e}")
                await asyncio.sleep(5)
                continue

            for app_row in apps_to_check:
                is_alive = bool(app_row.pid and _pid_alive(app_row.pid))

                if app_row.status == "running" and not is_alive:
                    logging.warning(
                        f"App '{app_row.name}' (PID {app_row.pid}) died unexpectedly."
                    )
                    try:
                        with Session(engine) as update_session:
                            refreshed = update_session.get(App, app_row.id)
                            if refreshed:
                                refreshed.pid = None
                                refreshed.status = "stopped"
                                refreshed.last_stop_time = utc_now()
                                if refreshed.last_exit_code is None:
                                    refreshed.last_exit_code = 1
                                update_session.add(refreshed)
                                update_session.commit()
                    except Exception as db_exc:
                        logging.error(
                            f"Failed to update state for {app_row.name}: {db_exc}"
                        )

                    if app_row.restart_policy in ("always", "on-failure"):
                        has_started_this_session = (
                            app_row.last_start_time is not None
                            and _make_tz_aware(app_row.last_start_time)
                            >= _make_tz_aware(_VENVY_BOOT_TIME)
                        )
                        if not has_started_this_session and not app_row.start_on_boot:
                            continue

                        was_manual = (
                            app_row.last_manually_stopped_at is not None
                            and app_row.last_start_time is not None
                            and _make_tz_aware(app_row.last_manually_stopped_at)
                            >= _make_tz_aware(app_row.last_start_time)
                        )
                        if not was_manual:
                            logging.info(
                                f"Restarting '{app_row.name}' (policy: {app_row.restart_policy})"
                            )
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(
                                        app_row.id, restart_session
                                    )
                            except Exception as exc:
                                logging.error(
                                    f"Restart failed for '{app_row.name}': {exc}"
                                )

                elif app_row.status == "running" and is_alive:
                    try:
                        detected_ports = await asyncio.to_thread(
                            _detect_ports_sync, app_row.pid
                        )
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
                                    logging.info(
                                        f"Ports updated to {combined} for '{app_row.name}'"
                                    )
                    except Exception as exc:
                        logging.debug(
                            f"Port detect scan failed for '{app_row.name}': {exc}"
                        )

                elif (
                    app_row.status == "stopped"
                    and app_row.restart_policy in ("always", "on-failure")
                    and app_row.last_start_time is not None
                    and (
                        app_row.last_stop_time is None
                        or (
                            utc_now() - _make_tz_aware(app_row.last_stop_time)
                        ).total_seconds()
                        >= 5.0
                    )
                ):

                    should_restart = False
                    if app_row.restart_policy == "always":
                        should_restart = True
                    elif (
                        app_row.restart_policy == "on-failure"
                        and app_row.last_exit_code != 0
                    ):
                        should_restart = True

                    if should_restart:
                        has_started_this_session = (
                            app_row.last_start_time is not None
                            and _make_tz_aware(app_row.last_start_time)
                            >= _make_tz_aware(_VENVY_BOOT_TIME)
                        )
                        if not has_started_this_session and not app_row.start_on_boot:
                            continue

                        was_manual = (
                            app_row.last_manually_stopped_at is not None
                            and _make_tz_aware(app_row.last_manually_stopped_at)
                            >= _make_tz_aware(app_row.last_start_time)
                        )
                        if not was_manual:
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(
                                        app_row.id, restart_session
                                    )
                            except Exception:
                                pass

        except Exception as exc:
            logging.error(f"Supervisor loop error: {exc}")
