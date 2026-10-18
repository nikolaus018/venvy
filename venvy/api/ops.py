"""Operations routes (admin): audit trail, shared environment variables, resource
history and alerts, notification channels, scheduled backups and the off-site copy."""
import datetime
import re
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, col, select

from venvy import monitor, notify, remote_backup, scheduler, shared_env
from venvy.audit import audit_log
from venvy.auth import check_admin, check_ip_allowlist, rate_limit_mutations
from venvy.backup import _resolve_backup_path
from venvy.config import utc_now
from venvy.cron import CronError, validate as validate_cron
from venvy.db import get_session
from venvy.jobs import _start_job
from venvy.models import AlertRule, App, AuditEntry, NotifyChannel, User

router = APIRouter(
    prefix="/api", tags=["ops"], dependencies=[Depends(check_ip_allowlist)]
)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

@router.get("/audit")
def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: str = "",
    action: str = "",
    q: str = "",
    since_hours: int = Query(default=0, ge=0, le=24 * 365),
    session: Session = Depends(get_session),
    _: User = Depends(check_admin),
):
    """Who did what, newest first. `q` searches the detail; `action` matches a prefix."""
    conds = []
    if user:
        conds.append(AuditEntry.username == user)
    if action:
        conds.append(col(AuditEntry.action).startswith(action))
    if q:
        conds.append(col(AuditEntry.detail).contains(q))
    if since_hours:
        conds.append(AuditEntry.ts >= utc_now() - datetime.timedelta(hours=since_hours))
    total = session.exec(select(func.count()).select_from(AuditEntry).where(*conds)).one()
    rows = session.exec(select(AuditEntry).where(*conds).order_by(AuditEntry.id.desc()).offset(offset).limit(limit)).all()
    return {
        "total": total,
        "entries": [{"id": r.id, "ts": r.ts.isoformat(), "user": r.username, "action": r.action,
                     "detail": r.detail, "ip": r.ip} for r in rows],
    }


# ---------------------------------------------------------------------------
# Shared environment variables
# ---------------------------------------------------------------------------

class SharedEnvReq(BaseModel):
    vars: Dict[str, str]


def check_env_vars(values: Dict[str, str]) -> Dict[str, str]:
    for k, v in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise HTTPException(400, f"Invalid environment variable name: {k!r}")
        if "\x00" in v:
            raise HTTPException(400, f"Environment variable {k} contains a NUL character.")
    return values


@router.get("/system/shared-env")
def get_shared_env(_: User = Depends(check_admin)):
    """Variables every app receives (an app's own variables win)."""
    return {"vars": shared_env.load(), "unreadable": not shared_env.readable()}


@router.put("/system/shared-env")
def put_shared_env(req: SharedEnvReq, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    values = check_env_vars(req.vars)
    old = shared_env.load()
    shared_env.save(values)
    audit_log(admin.username, "shared_env_change",
              f"added={sorted(set(values) - set(old))} removed={sorted(set(old) - set(values))} "
              f"changed={sorted(k for k in values if k in old and old[k] != values[k])}")
    return {"ok": True, "count": len(values)}


# ---------------------------------------------------------------------------
# History and alerts
# ---------------------------------------------------------------------------

@router.get("/system/metrics/history")
def host_metrics_history(minutes: int = Query(default=60, ge=1, le=10080), _: User = Depends(check_admin)):
    return {"minutes": minutes, "samples": monitor.history(0, minutes)}


class AlertReq(BaseModel):
    app_id: int = 0
    metric: str
    threshold: float
    minutes: int = 5
    enabled: bool = True


def _alert_json(rule: AlertRule, names: Dict[int, str]) -> dict:
    return {"id": rule.id, "app_id": rule.app_id, "app": names.get(rule.app_id), "metric": rule.metric,
            "threshold": rule.threshold, "minutes": rule.minutes, "enabled": rule.enabled,
            "firing": rule.firing, "since": rule.since.isoformat() if rule.since else None,
            "description": monitor.describe_rule(rule, names.get(rule.app_id))}


@router.get("/system/alerts")
def list_alerts(session: Session = Depends(get_session), _: User = Depends(check_admin)):
    names = {a.id: a.name for a in session.exec(select(App)).all()}
    return [_alert_json(r, names) for r in session.exec(select(AlertRule).order_by(AlertRule.id)).all()]


def _validated_rule(req: AlertReq, session: Session) -> None:
    if req.app_id and not session.get(App, req.app_id):
        raise HTTPException(404, "No such app.")
    try:
        monitor.check_rule(req.app_id, req.metric, req.threshold, req.minutes)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/system/alerts")
def create_alert(req: AlertReq, session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    _validated_rule(req, session)
    rule = AlertRule(app_id=req.app_id, metric=req.metric, threshold=req.threshold, minutes=req.minutes, enabled=req.enabled)
    session.add(rule)
    session.commit()
    session.refresh(rule)
    audit_log(admin.username, "alert_create", monitor.describe_rule(rule, None))
    return _alert_json(rule, {a.id: a.name for a in session.exec(select(App)).all()})


@router.put("/system/alerts/{rule_id}")
def update_alert(rule_id: int, req: AlertReq, session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    rule = session.get(AlertRule, rule_id)
    if not rule:
        raise HTTPException(404)
    _validated_rule(req, session)
    rule.app_id, rule.metric, rule.threshold, rule.minutes, rule.enabled = req.app_id, req.metric, req.threshold, req.minutes, req.enabled
    rule.firing, rule.since = False, None
    session.add(rule)
    session.commit()
    audit_log(admin.username, "alert_update", monitor.describe_rule(rule, None))
    return _alert_json(rule, {a.id: a.name for a in session.exec(select(App)).all()})


@router.delete("/system/alerts/{rule_id}")
def delete_alert(rule_id: int, session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    rule = session.get(AlertRule, rule_id)
    if rule:
        session.delete(rule)
        session.commit()
        audit_log(admin.username, "alert_delete", f"id={rule_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Notification channels
# ---------------------------------------------------------------------------

class ChannelReq(BaseModel):
    name: str
    kind: str
    config: Dict[str, object] = {}
    events: str = "*"
    enabled: bool = True


def _channel_json(ch: NotifyChannel) -> dict:
    cfg = notify.load_config(ch)
    return {"id": ch.id, "name": ch.name, "kind": ch.kind, "events": ch.events, "enabled": ch.enabled,
            "config": notify.masked(ch.kind, cfg) if ch.kind in notify.FIELDS else {},
            "last_status": ch.last_status, "last_sent_at": ch.last_sent_at.isoformat() if ch.last_sent_at else None}


def _clean_events(events: str) -> str:
    names = [e.strip() for e in (events or "*").split(",") if e.strip()] or ["*"]
    if "*" in names:
        return "*"
    for n in names:
        if n not in notify.EVENTS:
            raise HTTPException(400, f"Unknown event '{n}'.")
    return ",".join(names)


@router.get("/system/notify")
def list_channels(session: Session = Depends(get_session), _: User = Depends(check_admin)):
    return {"events": notify.EVENTS, "kinds": list(notify.KINDS),
            "required": {k: list(v["required"]) for k, v in notify.FIELDS.items()},
            "channels": [_channel_json(c) for c in session.exec(select(NotifyChannel).order_by(NotifyChannel.id)).all()]}


@router.post("/system/notify")
def create_channel(req: ChannelReq, session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    name = req.name.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "A channel needs a name of up to 60 characters.")
    try:
        cfg = notify.check_config(req.kind, dict(req.config))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    ch = NotifyChannel(name=name, kind=req.kind, config=notify.store_config(cfg), events=_clean_events(req.events), enabled=req.enabled)
    session.add(ch)
    session.commit()
    session.refresh(ch)
    audit_log(admin.username, "notify_create", f"{req.kind} channel '{name}'")
    return _channel_json(ch)


@router.put("/system/notify/{channel_id}")
def update_channel(channel_id: int, req: ChannelReq, session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    ch = session.get(NotifyChannel, channel_id)
    if not ch:
        raise HTTPException(404)
    name = req.name.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "A channel needs a name of up to 60 characters.")
    old = notify.load_config(ch) if ch.kind == req.kind else {}
    try:
        cfg = notify.check_config(req.kind, notify.merge_secrets(req.kind, dict(req.config), old))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    ch.name, ch.kind, ch.config = name, req.kind, notify.store_config(cfg)
    ch.events, ch.enabled = _clean_events(req.events), req.enabled
    session.add(ch)
    session.commit()
    audit_log(admin.username, "notify_update", f"{req.kind} channel '{name}'")
    return _channel_json(ch)


@router.delete("/system/notify/{channel_id}")
def delete_channel(channel_id: int, session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    ch = session.get(NotifyChannel, channel_id)
    if ch:
        session.delete(ch)
        session.commit()
        audit_log(admin.username, "notify_delete", f"channel '{ch.name}'")
    return {"ok": True}


@router.post("/system/notify/{channel_id}/test")
async def test_channel(channel_id: int, _: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    res = await notify.send_test(channel_id)
    return {"ok": res["status"] == "ok", "status": res["status"]}


# ---------------------------------------------------------------------------
# Scheduled backups and the off-site copy
# ---------------------------------------------------------------------------

class BackupScheduleReq(BaseModel):
    schedule: str = ""
    keep: int = 7


@router.get("/system/backup/schedule")
def get_backup_schedule(_: User = Depends(check_admin)):
    return scheduler.backup_settings()


@router.put("/system/backup/schedule")
def put_backup_schedule(req: BackupScheduleReq, admin: User = Depends(check_admin)):
    expr = req.schedule.strip()
    if expr:
        try:
            expr = validate_cron(expr)
        except CronError as exc:
            raise HTTPException(400, str(exc))
    if not (1 <= req.keep <= 365):
        raise HTTPException(400, "keep must be between 1 and 365.")
    scheduler.set_setting(scheduler.SCHEDULE_KEY, expr)
    scheduler.set_setting(scheduler.KEEP_KEY, str(req.keep))
    audit_log(admin.username, "backup_schedule_change", f"schedule={expr or 'off'} keep={req.keep}")
    return scheduler.backup_settings()


@router.put("/system/backup/remote")
def put_backup_remote(cfg: Dict[str, object], admin: User = Depends(check_admin)):
    try:
        clean = remote_backup.clean(dict(cfg), remote_backup.load())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    remote_backup.save(clean)
    audit_log(admin.username, "backup_remote_change", f"kind={clean['kind']}")
    return {"remote": remote_backup.masked(clean)}


@router.delete("/system/backup/remote")
def delete_backup_remote(admin: User = Depends(check_admin)):
    remote_backup.save(None)
    audit_log(admin.username, "backup_remote_change", "removed")
    return {"ok": True}


@router.post("/system/backup/remote/test")
async def test_backup_remote(cfg: Optional[Dict[str, object]] = None, _: User = Depends(check_admin),
                             _rl: None = Depends(rate_limit_mutations)):
    """Upload a small file with the saved settings (or the ones in the body, unsaved)."""
    try:
        use = remote_backup.clean(dict(cfg), remote_backup.load()) if cfg else remote_backup.load()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not use:
        raise HTTPException(400, "No off-site copy is set up.")
    try:
        where = await remote_backup.test(use)
    except remote_backup.RemoteError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "where": where}


@router.post("/system/backups/{filename}/upload")
async def upload_backup_offsite(filename: str, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    """Copy an existing backup to the off-site place. Answers {"job_id"}."""
    path = _resolve_backup_path(filename)
    if not remote_backup.load():
        raise HTTPException(400, "No off-site copy is set up.")

    async def _job() -> dict:
        try:
            where = await remote_backup.upload(path)
        except remote_backup.RemoteError as exc:
            raise HTTPException(500, str(exc))
        audit_log(admin.username, "backup_remote_upload", f"{filename} -> {where}")
        return {"ok": True, "where": where}

    return _start_job(admin, "backup_remote_upload", _job)


@router.post("/system/backup/schedule/run")
async def run_scheduled_backup_now(admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    """Run the scheduled backup (retention and off-site copy included) right now."""
    scheduler.start_scheduled_backup()
    audit_log(admin.username, "backup_schedule_run", "")
    return {"ok": True}
