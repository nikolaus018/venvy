"""Things that run on a schedule: each app's cron jobs, and Venvy's own backups.

One loop wakes every few seconds; when the clock reaches a new minute it
starts whatever matches. As with cron, a run that was due while Venvy was
down is skipped, and a job that is still running is not started a second time.
Times are the server's local time."""
import asyncio
import contextlib
import datetime
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Dict, List, Optional

from sqlmodel import Session, select

from venvy import notify, remote_backup
from venvy.audit import audit_log
from venvy.config import BACKUP_DIR, utc_now
from venvy.cron import Cron, CronError
from venvy.db import engine
from venvy.isolation import _as_app_user, _ensure_app_user
from venvy.jobs import _running_tasks
from venvy.models import App, ScheduledJob, SystemSetting

OUTPUT_KEEP = 8000
SCHEDULE_KEY = "backup_schedule"       # cron expression; empty = off
KEEP_KEY = "backup_keep"               # how many scheduled backups to keep
NAMES_KEY = "backup_scheduled_names"   # JSON list: the files the schedule made (backup.SCHEDULED_NAMES_KEY)
LAST_KEY = "backup_last_scheduled"     # JSON: how the last scheduled run went

_running: Dict[int, asyncio.Task] = {}
_fired_minute: Dict[object, tuple] = {}


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with Session(engine) as s:
        row = s.get(SystemSetting, key)
    return row.value if row and row.value is not None else default


def set_setting(key: str, value: str) -> None:
    with Session(engine) as s:
        row = s.get(SystemSetting, key) or SystemSetting(key=key)
        row.value = value
        s.add(row)
        s.commit()


def backup_settings() -> dict:
    keep = 7
    with contextlib.suppress(ValueError):
        keep = int(get_setting(KEEP_KEY, "7"))
    last = None
    with contextlib.suppress(ValueError):
        last = json.loads(get_setting(LAST_KEY, "") or "null")
    sched = get_setting(SCHEDULE_KEY, "")
    nxt = None
    if sched:
        with contextlib.suppress(CronError):
            nxt = Cron(sched).next_after(datetime.datetime.now()).isoformat()
    return {"schedule": sched, "keep": keep, "last": last, "next_run": nxt, "remote": remote_backup.masked(remote_backup.load())}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def next_run(job: ScheduledJob) -> Optional[str]:
    try:
        return Cron(job.schedule).next_after(datetime.datetime.now()).isoformat()
    except CronError:
        return None


def is_running(job_id: int) -> bool:
    t = _running.get(job_id)
    return bool(t and not t.done())


async def run_job(job_id: int, by: str = "scheduler") -> dict:
    """Run a job now and wait for it. Raises ValueError if it is already running."""
    if is_running(job_id):
        raise ValueError("This job is still running.")
    task = asyncio.ensure_future(_run_job(job_id, by))
    _running[job_id] = task
    try:
        return await task
    finally:
        if _running.get(job_id) is task:
            _running.pop(job_id, None)


def start_job(job_id: int, by: str) -> None:
    """Run a job in the background."""
    if is_running(job_id):
        raise ValueError("This job is still running.")
    task = asyncio.ensure_future(_run_job(job_id, by))
    _running[job_id] = task
    task.add_done_callback(lambda t: _finished(job_id, t))


def _finished(job_id: int, task: asyncio.Task) -> None:
    if _running.get(job_id) is task:
        _running.pop(job_id, None)
    if not task.cancelled() and task.exception():
        logging.error(f"Job {job_id} crashed: {task.exception()}")


def _mask_env_secrets(text: str, app: App, extra_secrets: Optional[List[str]] = None) -> str:
    """*text* with the app's environment values masked, as in a deploy log.
    Uses strict=True so a transient DB/decryption failure fails closed."""
    from venvy.deploy import _env_secrets
    try:
        secrets = set(_env_secrets(app, strict=True))
    except Exception as exc:
        logging.error(f"Could not load the secrets of app {app.id}: {exc}")
        return "[venvy] output hidden: the app's secrets could not be loaded to mask it"
    if extra_secrets:
        secrets.update(s for s in extra_secrets if s)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def host_app() -> App:
    """The stand-in app host-level jobs run as: user venvy-app-0 (under per-app
    users), folder DATA_DIR/host-jobs, no variables but the shared ones."""
    from venvy.config import DATA_DIR
    d = DATA_DIR / "host-jobs"
    d.mkdir(mode=0o750, exist_ok=True)
    return App(id=0, user_id=0, name="host", slug="host", runtime="command", app_dir=str(d),
               venv_path=str(d / "venv"), entrypoint="", env_json="{}")


async def run_shell(app, command: str, timeout: int) -> dict:
    """Run *command* with sh as the app's user, in its working folder, with its
    environment; {"status": ok|failed|timeout, "exit_code", "duration", "output"}
    (output masked for secrets, the last OUTPUT_KEEP characters)."""
    from venvy import appspec
    from venvy.deploy import _app_env_for_commands, _MASK_MIN_LEN
    from venvy.isolation import _clean_root_env, _exec_env_argv, _isolation_active, _prepare_exec_env
    started = time.monotonic()
    out_tail, status, code = "", "failed", None
    captured_secrets: List[str] = []
    proc = None
    try:
        await asyncio.to_thread(_ensure_app_user, app)
        job_env = await asyncio.to_thread(_app_env_for_commands, app)
        captured_secrets = [v for v in job_env.values() if isinstance(v, str) and len(v) >= _MASK_MIN_LEN]
        env_tf = None
        try:
            if _isolation_active():
                env_tf, env_fd = _prepare_exec_env(job_env)
                spawn_argv = _as_app_user(app, _exec_env_argv(env_fd, ["sh", "-c", command]))
                spawn_env = _clean_root_env()
                pass_fds = (env_fd,)
            else:
                spawn_argv = ["sh", "-c", command]
                spawn_env = job_env
                pass_fds = ()
            wd = appspec.work_dir(app)
            proc = await asyncio.create_subprocess_exec(
                *spawn_argv,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env=spawn_env, cwd=str(wd if wd.is_dir() else app.app_dir), start_new_session=True, pass_fds=pass_fds)
        finally:
            if env_tf is not None:
                env_tf.close()
        chunks: List[bytes] = []
        size = 0

        async def pump() -> None:
            nonlocal size
            assert proc.stdout is not None
            while True:
                data = await proc.stdout.read(8192)
                if not data:
                    return
                chunks.append(data)
                size += len(data)
                while size > OUTPUT_KEEP * 4 and len(chunks) > 1:      # keep memory bounded
                    size -= len(chunks.pop(0))

        try:
            await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
            code = proc.returncode
            status = "ok" if code == 0 else "failed"
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            status = "timeout"
        # Masked before the cut, so a secret across it can't survive in part.
        out_tail = _mask_env_secrets(b"".join(chunks).decode(errors="replace"), app, captured_secrets)[-OUTPUT_KEEP:]
        if status == "timeout":
            out_tail += f"\n[venvy] killed after {timeout} seconds"
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    except Exception as exc:
        out_tail = _mask_env_secrets(f"[venvy] could not run the command: {exc}", app, captured_secrets)
        status = "failed"
    return {"status": status, "exit_code": code, "duration": round(time.monotonic() - started, 2), "output": out_tail}


async def _run_job(job_id: int, by: str) -> dict:
    def _start_record():
        with Session(engine, expire_on_commit=False) as s:
            job = s.get(ScheduledJob, job_id)
            if not job:
                return None, None, None
            app = s.get(App, job.app_id) if job.app_id else host_app()
            if not app:
                return None, None, None
            job.last_status, job.last_run_at = "running", utc_now()
            s.add(job)
            s.commit()
            return job, app, (job.command, job.timeout, job.name)

    job, app, meta = await asyncio.to_thread(_start_record)
    if not job or not app or not meta:
        return {"status": "failed", "output": "The job or its app no longer exists."}
    command, timeout, name = meta
    try:
        res = await run_shell(app, command, timeout)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            with Session(engine) as s:
                row = s.get(ScheduledJob, job_id)
                if row and row.last_status == "running":
                    row.last_status = "failed"
                    row.last_output = "[venvy] job cancelled during shutdown"
                    s.add(row)
                    s.commit()
        raise
    status, code, duration, out_tail = res["status"], res["exit_code"], res["duration"], res["output"]

    def _finish_record() -> None:
        with Session(engine) as s:
            row = s.get(ScheduledJob, job_id)
            if row:
                row.last_status, row.last_exit_code, row.last_duration, row.last_output = status, code, duration, out_tail
                s.add(row)
                s.commit()

    await asyncio.to_thread(_finish_record)
    if status != "ok":
        notify.emit("job_failed", f"{app.name}: job '{name}' {'timed out' if status == 'timeout' else 'failed'}",
                    out_tail[-600:], app.id or None, app.name, "error")
    return res


# ---------------------------------------------------------------------------
# Scheduled backups
# ---------------------------------------------------------------------------

class _Who:
    username = "scheduler"


async def run_scheduled_backup() -> None:
    from venvy.backup import _FULL_BACKUP_FILENAME_RE, _backup_job, _begin_operation
    from fastapi import HTTPException
    result = {"at": utc_now().isoformat(), "ok": False, "file": None, "size": 0, "remote": None, "error": None}
    try:
        _begin_operation("backup", "scheduler")
    except HTTPException as exc:
        logging.info(f"Scheduled backup skipped: {exc.detail}")
        return
    try:
        out = await _backup_job(_Who(), scheduled=True)           # type: ignore[arg-type]
        path = Path(out["path"])
        result.update(ok=True, file=path.name, size=out["size_bytes"])
        names = []
        with contextlib.suppress(ValueError):
            names = json.loads(get_setting(NAMES_KEY, "[]"))
        # Only names of backups: the setting is also writable through the
        # generic settings API, and each name pruned below is deleted.
        names = [n for n in names if isinstance(n, str) and _FULL_BACKUP_FILENAME_RE.match(n)] \
            if isinstance(names, list) else []
        names.append(path.name)
        keep = max(1, backup_settings()["keep"])
        while len(names) > keep:
            old = names.pop(0)
            with contextlib.suppress(OSError):
                (BACKUP_DIR / old).unlink()
        set_setting(NAMES_KEY, json.dumps(names))
        cfg = remote_backup.load()
        if cfg:
            try:
                result["remote"] = "ok: " + await remote_backup.upload(path, cfg)
            except remote_backup.RemoteError as exc:
                result["remote"] = f"failed: {exc}"
                notify.emit("backup_failed", "Off-site copy of the backup failed", str(exc), level="error")
        notify.emit("backup_ok", "Scheduled backup finished", f"{path.name} ({out['size_bytes'] // 1048576} MB)"
                    + (f"\nOff-site: {result['remote']}" if result["remote"] else ""))
    except Exception as exc:
        result["error"] = getattr(exc, "detail", None) or str(exc)
        logging.error(f"Scheduled backup failed: {result['error']}")
        notify.emit("backup_failed", "Scheduled backup failed", str(result["error"]), level="error")
    set_setting(LAST_KEY, json.dumps(result))
    audit_log("scheduler", "system_scheduled_backup", "ok" if result["ok"] else f"failed: {result['error']}")


def start_scheduled_backup() -> None:
    """run_scheduled_backup in the background, holding on to the task so it is not collected mid-run."""
    t = asyncio.ensure_future(run_scheduled_backup())
    _running_tasks.add(t)
    t.add_done_callback(_running_tasks.discard)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def tick(now: Optional[datetime.datetime] = None) -> None:
    """Start whatever is due in the current minute (once)."""
    now = (now or datetime.datetime.now()).replace(second=0, microsecond=0)
    from venvy.process import restore_in_progress
    if restore_in_progress():
        return

    def _load_due_data():
        with Session(engine) as s:
            jobs_list = s.exec(select(ScheduledJob).where(ScheduledJob.enabled == True)).all()  # noqa: E712
        return jobs_list, get_setting(SCHEDULE_KEY, "")

    jobs, sched = await asyncio.to_thread(_load_due_data)
    active_ids = {j.id for j in jobs}
    for k in [k for k in _fired_minute if k != "backup" and k not in active_ids]:
        _fired_minute.pop(k, None)
    for job in jobs:
        if _fired_minute.get(job.id) == now.timetuple()[:5]:
            continue
        try:
            due = Cron(job.schedule).matches(now)
        except CronError:
            continue
        if due:
            _fired_minute[job.id] = now.timetuple()[:5]
            try:
                start_job(job.id, "scheduler")
            except ValueError:
                logging.info(f"Job '{job.name}' is still running; this run is skipped.")
    if sched and _fired_minute.get("backup") != now.timetuple()[:5]:
        try:
            due = Cron(sched).matches(now)
        except CronError:
            due = False
        if due:
            _fired_minute["backup"] = now.timetuple()[:5]
            start_scheduled_backup()


async def scheduler_task() -> None:
    while True:
        try:
            await tick()
        except Exception as exc:
            logging.error(f"Scheduler error: {exc}")
        await asyncio.sleep(5)
