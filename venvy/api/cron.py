"""Scheduled jobs of an app: a command, run on a cron schedule as the app's user."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from venvy import scheduler
from venvy.audit import audit_log
from venvy.auth import check_ip_allowlist, get_current_user, rate_limit_mutations, require_app_access, require_operator
from venvy.cron import CronError, validate
from venvy.db import get_session
from venvy.models import App, ScheduledJob, User

router = APIRouter(
    prefix="/api", tags=["jobs"], dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)]
)

MAX_JOBS_PER_APP = 50


class JobReq(BaseModel):
    name: str
    schedule: str
    command: str
    timeout: int = 600
    enabled: bool = True


def _json(job: ScheduledJob) -> dict:
    return {
        "id": job.id, "name": job.name, "schedule": job.schedule, "command": job.command,
        "timeout": job.timeout, "enabled": job.enabled,
        "next_run": scheduler.next_run(job) if job.enabled else None,
        "running": scheduler.is_running(job.id),
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_status": job.last_status, "last_exit_code": job.last_exit_code,
        "last_duration": job.last_duration, "last_output": job.last_output,
    }


def _clean(req: JobReq) -> dict:
    name = req.name.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "A job needs a name of up to 60 characters.")
    command = req.command.strip()
    if not command or len(command) > 2000 or "\0" in command:
        raise HTTPException(400, "A job needs a command (up to 2000 characters).")
    if not (5 <= req.timeout <= 86400):
        raise HTTPException(400, "The timeout must be between 5 seconds and 24 hours.")
    try:
        schedule = validate(req.schedule)
    except CronError as exc:
        raise HTTPException(400, str(exc))
    return {"name": name, "command": command, "timeout": req.timeout, "schedule": schedule, "enabled": req.enabled}


def _job(session: Session, app_id: int, job_id: int) -> ScheduledJob:
    job = session.get(ScheduledJob, job_id)
    if not job or job.app_id != app_id:
        raise HTTPException(404)
    return job


@router.get("/apps/{app_id}/jobs")
def list_jobs(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    if not session.get(App, app_id):
        raise HTTPException(404)
    can_see_commands = user.role in ("admin", "operator")
    out = []
    for j in session.exec(select(ScheduledJob).where(ScheduledJob.app_id == app_id).order_by(ScheduledJob.id)).all():
        d = _json(j)
        if not can_see_commands:           # a command may carry secrets; a viewer sees that jobs exist
            d["command"], d["last_output"] = "", ""
        out.append(d)
    return out


@router.post("/apps/{app_id}/jobs")
def create_job(app_id: int, req: JobReq, session: Session = Depends(get_session), user: User = Depends(require_operator),
               _rl: None = Depends(rate_limit_mutations)):
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(404)
    if len(session.exec(select(ScheduledJob).where(ScheduledJob.app_id == app_id)).all()) >= MAX_JOBS_PER_APP:
        raise HTTPException(400, f"An app can have {MAX_JOBS_PER_APP} jobs at most.")
    job = ScheduledJob(app_id=app_id, **_clean(req))
    session.add(job)
    session.commit()
    session.refresh(job)
    audit_log(user.username, "job_create", f"app '{app.name}' (id={app_id}): '{job.name}' {job.schedule}")
    return _json(job)


@router.put("/apps/{app_id}/jobs/{job_id}")
def update_job(app_id: int, job_id: int, req: JobReq, session: Session = Depends(get_session),
               user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    job = _job(session, app_id, job_id)
    for k, v in _clean(req).items():
        setattr(job, k, v)
    session.add(job)
    session.commit()
    audit_log(user.username, "job_update", f"app id={app_id}: '{job.name}' {job.schedule}")
    return _json(job)


@router.delete("/apps/{app_id}/jobs/{job_id}")
def delete_job(app_id: int, job_id: int, session: Session = Depends(get_session),
               user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    job = _job(session, app_id, job_id)
    session.delete(job)
    session.commit()
    audit_log(user.username, "job_delete", f"app id={app_id}: '{job.name}'")
    return {"ok": True}


@router.post("/apps/{app_id}/jobs/{job_id}/run")
async def run_job_now(app_id: int, job_id: int, session: Session = Depends(get_session),
                user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    """Start the job now, in the background; its result shows in the job list."""
    job = _job(session, app_id, job_id)
    try:
        scheduler.start_job(job.id, user.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    audit_log(user.username, "job_run", f"app id={app_id}: '{job.name}'")
    return {"ok": True}
