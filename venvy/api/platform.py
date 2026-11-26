"""Routes for what makes Venvy host more than one process per app: processes,
routes and controls, port forwards, volumes, managed services, toolchains,
secrets back-ends, log shipping, metrics, monitors and the status page, the
TLS / Tailscale / ngrok edge, database replication, other servers, app
backups, one-off commands and artifact deploys."""
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from venvy import (appbackup, appspec, deploy, edge, federation, firewall, forwards, groups, kv, logship, own_hosts,
                   proxy, replication, routing, runtimes, scheduler, secrets_ext, services, telemetry, toolchains,
                   volumes)
from venvy.apps import _app_safe, _clean_app_name, sanitize_slug
from venvy.audit import audit_log
from venvy.auth import (check_admin, check_ip_allowlist, get_current_user, get_download_user, has_app_access,
                        rate_limit_mutations, require_app_access, require_operator)
from venvy.config import APPS_BASE_DIR, DATA_DIR, one_line, utc_now
from venvy.crypto import _encrypt_secret
from venvy.db import engine, get_session
from venvy.jobs import _start_job
from venvy.models import (App, AppAccess, AppVolume, PortForward, RemoteServer, Route, ServiceBinding,
                          ScheduledJob, UrlMonitor, User, Volume)

router = APIRouter(prefix="/api", tags=["platform"],
                   dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)])
public = APIRouter(tags=["public"])

_HEADER_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~\-]{1,64}")


def _app(session: Session, app_id: int) -> App:
    a = session.get(App, app_id)
    if a is None:
        raise HTTPException(404)
    return a


def _main(session: Session, app_id: int) -> App:
    a = _app(session, app_id)
    if a.parent_app_id:
        raise HTTPException(400, "This is a process of another app; use that app.")
    return a


# ---------------------------------------------------------------------------
# Processes (workers, replicas)
# ---------------------------------------------------------------------------

class ProcessReq(BaseModel):
    name: str
    command: str


@router.get("/apps/{app_id}/processes")
def list_processes(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _main(session, app_id)
    reveal = user.role in ("admin", "operator")
    return [_app_safe(c, reveal_secrets=reveal) for c in groups.children(session, app_id)]


@router.post("/apps/{app_id}/processes")
def add_process(app_id: int, req: ProcessReq, session: Session = Depends(get_session),
                user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    """A worker: another command of the app's code, supervised like the app."""
    parent = _main(session, app_id)
    name = req.name.strip()
    if not appspec._PROC_NAME_RE.fullmatch(name) or name == "web":
        raise HTTPException(400, "A process name is letters, digits, - and _ (and not 'web').")
    try:
        cmd = appspec.clean_command(req.command, "command")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not cmd:
        raise HTTPException(400, "A worker needs a command.")
    if any(c.process_name == name for c in groups.children(session, app_id, "worker")):
        raise HTTPException(409, f"There is already a process called {name}.")
    procs = {c.process_name: c.start_command for c in groups.children(session, app_id, "worker")}
    procs[name] = cmd
    groups.sync_workers(session, parent, procs)
    audit_log(user.username, "app_process_add", f"app '{parent.name}' (id={app_id}): {name}")
    return list_processes(app_id, session, user)


# ---------------------------------------------------------------------------
# Detecting the runtime
# ---------------------------------------------------------------------------

@router.get("/apps/{app_id}/detect")
def detect(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    a = _app(session, app_id)
    rt, entry, why = appspec.detect_runtime(appspec.work_dir(a))
    return {"runtime": rt, "entrypoint": entry, "reason": why}


# ---------------------------------------------------------------------------
# One-off commands
# ---------------------------------------------------------------------------

class RunReq(BaseModel):
    command: str
    timeout: int = 600


@router.post("/apps/{app_id}/run")
async def run_command(app_id: int, req: RunReq, session: Session = Depends(get_session),
                      user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    """Run a command once as the app (in its folder, with its variables) — migrations,
    a console script, a check. Answers {"job_id"}; the job's result has the output."""
    a = _app(session, app_id)
    if a.status == "installing":
        raise HTTPException(409, "The app is still being installed.")
    cmd = (req.command or "").strip()
    if not cmd or len(cmd) > 4000 or "\0" in cmd:
        raise HTTPException(400, "Give a command (up to 4000 characters).")
    if not 5 <= req.timeout <= 86400:
        raise HTTPException(400, "The timeout must be between 5 seconds and 24 hours.")
    spec = groups.effective(a, session)
    audit_log(user.username, "app_run_command", f"app '{a.name}' (id={app_id})")

    async def job() -> dict:
        return await scheduler.run_shell(spec, cmd, req.timeout)
    return _start_job(user, "app_run", job, app_id=app_id, min_role="operator")


# ---------------------------------------------------------------------------
# Host-level scheduled jobs (admin)
# ---------------------------------------------------------------------------

@router.get("/system/jobs")
def host_jobs(session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    from venvy.api.cron import _json
    return [_json(j) for j in session.exec(select(ScheduledJob).where(ScheduledJob.app_id == 0)).all()]


@router.post("/system/jobs")
def add_host_job(req: dict, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                 _rl: None = Depends(rate_limit_mutations)):
    """A job that belongs to no app: runs as the unprivileged user venvy-app-0 in DATA_DIR/host-jobs."""
    from venvy.api.cron import JobReq, _clean, _json
    try:
        j = ScheduledJob(app_id=0, **_clean(JobReq(**req)))
    except TypeError:
        raise HTTPException(400, "name, schedule and command are needed.")
    session.add(j)
    session.commit()
    session.refresh(j)
    audit_log(admin.username, "host_job_create", f"'{j.name}' {j.schedule}")
    return _json(j)


@router.put("/system/jobs/{job_id}")
def put_host_job(job_id: int, req: dict, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                 _rl: None = Depends(rate_limit_mutations)):
    from venvy.api.cron import JobReq, _clean, _json
    j = session.get(ScheduledJob, job_id)
    if not j or j.app_id != 0:
        raise HTTPException(404)
    for k, v in _clean(JobReq(**req)).items():
        setattr(j, k, v)
    session.add(j)
    session.commit()
    return _json(j)


@router.delete("/system/jobs/{job_id}")
def del_host_job(job_id: int, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                 _rl: None = Depends(rate_limit_mutations)):
    j = session.get(ScheduledJob, job_id)
    if not j or j.app_id != 0:
        raise HTTPException(404)
    session.delete(j)
    session.commit()
    audit_log(admin.username, "host_job_delete", f"'{j.name}'")
    return {"ok": True}


@router.post("/system/jobs/{job_id}/run")
async def run_host_job(job_id: int, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                 _rl: None = Depends(rate_limit_mutations)):
    j = session.get(ScheduledJob, job_id)
    if not j or j.app_id != 0:
        raise HTTPException(404)
    try:
        scheduler.start_job(j.id, admin.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Routes (hostnames, paths, redirects, controls)
# ---------------------------------------------------------------------------

class RouteReq(BaseModel):
    hostname: str
    path_prefix: str = "/"
    strip_prefix: bool = False
    kind: str = "proxy"
    redirect_to: Optional[str] = None
    redirect_code: int = 301
    basic_auth: Optional[Dict[str, str]] = None      # {user: password}; None keeps, {} clears
    ip_allow: str = ""
    rate_limit: int = 0
    max_body_mb: int = 0
    headers: Dict[str, str] = {}
    gzip: bool = False
    cache_seconds: int = 0
    tls: bool = True


def _route_json(r: Route) -> dict:
    d = r.model_dump()
    d["created_at"] = r.created_at.isoformat() if r.created_at else None
    d["headers"] = json.loads(r.headers or "{}")
    users = routing._users(r.basic_auth)
    d["basic_auth"] = sorted(u for u in users if not u.startswith("\0"))
    return d


def _clean_route(session: Session, req: RouteReq, app_id: int, route_id: Optional[int], request: Request,
                 user: User) -> dict:
    host = req.hostname.strip().lower().rstrip(".")
    if not own_hosts.valid_hostname(host, wildcard=True):
        raise HTTPException(400, "The hostname must look like app.example.com or *.example.com.")
    if own_hosts.is_own(host) or host == own_hosts.host_name(request.headers.get("host", "")):
        raise HTTPException(400, "That is an address Venvy itself is reached under.")
    prefix = "/" + req.path_prefix.strip().strip("/") if req.path_prefix.strip("/ ") else "/"
    if len(prefix) > 200 or not re.fullmatch(r"/[A-Za-z0-9._~\-/]*", prefix) or "//" in prefix or "/../" in prefix + "/":
        raise HTTPException(400, "The path must look like /api.")
    if req.kind not in ("proxy", "redirect"):
        raise HTTPException(400, "kind must be proxy or redirect.")
    redirect_to = None
    if req.kind == "redirect":
        redirect_to = (req.redirect_to or "").strip()
        if not redirect_to or len(redirect_to) > 500 or any(c in redirect_to for c in "\r\n \t<>\"'"):
            raise HTTPException(400, "A redirect needs a target: a hostname or https:// URL.")
        if "://" in redirect_to and not redirect_to.startswith(("https://", "http://")):
            raise HTTPException(400, "The redirect target must be http(s).")
        if req.redirect_code not in (301, 302, 303, 307, 308):
            raise HTTPException(400, "redirect_code must be 301, 302, 303, 307 or 308.")
    for other in session.exec(select(Route).where(Route.hostname == host, Route.path_prefix == prefix)).all():
        if other.id != route_id:
            raise HTTPException(409, f"{host}{'' if prefix == '/' else prefix} is already routed.")
    owner = session.exec(select(App).where(App.domain == host)).first()
    if owner and owner.id != app_id and prefix == "/":
        raise HTTPException(409, f"{host} is the domain of another app.")
    # Any path on a hostname another app is served under would take its
    # requests (the longest prefix wins): only for someone who manages that app too.
    for other_id in routing.hostname_apps(session, host, skip_route=route_id):
        if other_id != app_id and not has_app_access(session, user, other_id):
            raise HTTPException(409, f"{host} is used by an app you can't manage.")
    for net in [c.strip() for c in req.ip_allow.split(",") if c.strip()]:
        import ipaddress
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            raise HTTPException(400, f"'{net}' is not an IP address or range.")
    for k, v in req.headers.items():
        if not _HEADER_RE.fullmatch(k) or k.lower() in ("content-length", "transfer-encoding", "connection", "host") \
                or any(c in v for c in "\r\n\0") or len(v) > 2000:
            raise HTTPException(400, f"The header {k!r} can't be set.")
    if not 0 <= req.rate_limit <= 100000 or not 0 <= req.max_body_mb <= 100000 or not 0 <= req.cache_seconds <= 86400:
        raise HTTPException(400, "rate_limit, max_body_mb or cache_seconds is out of range.")
    out = dict(hostname=host, path_prefix=prefix, strip_prefix=req.strip_prefix, kind=req.kind,
               redirect_to=redirect_to, redirect_code=req.redirect_code, ip_allow=req.ip_allow.strip(),
               rate_limit=req.rate_limit, max_body_mb=req.max_body_mb, headers=json.dumps(req.headers),
               gzip=req.gzip, cache_seconds=req.cache_seconds, tls=req.tls)
    if req.basic_auth is not None:
        for u, pw in req.basic_auth.items():
            if not re.fullmatch(r"[A-Za-z0-9._@\-]{1,64}", u) or (not pw and not pw.startswith("scrypt$")) or len(pw) > 500:
                raise HTTPException(400, "Basic auth needs user names (letters, digits, . _ @ -) with passwords.")
        out["basic_auth"] = routing.encode_users(req.basic_auth)
    return out


@router.get("/apps/{app_id}/routes")
def list_routes(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _main(session, app_id)
    return [_route_json(r) for r in session.exec(select(Route).where(Route.app_id == app_id)).all()]


async def _check_route_port(session: Session, a: App, kind: str, request: Request) -> None:
    """A route sends a hostname's requests to the app's port, as a domain does:
    that port must be the app's (not Venvy's, another app's or a service that
    listens on localhost because it trusts this host)."""
    if kind == "proxy" and a.port:
        from venvy.api.cloudflare import _check_publish_port
        await _check_publish_port(session, a, a.port, request)


@router.post("/apps/{app_id}/routes")
async def add_route(app_id: int, req: RouteReq, request: Request, session: Session = Depends(get_session),
                    user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    if len(session.exec(select(Route).where(Route.app_id == app_id)).all()) >= 100:
        raise HTTPException(400, "An app can have 100 routes at most.")
    fields = _clean_route(session, req, app_id, None, request, user)
    await _check_route_port(session, a, fields["kind"], request)
    r = Route(app_id=app_id, **fields)
    session.add(r)
    session.commit()
    session.refresh(r)
    proxy.invalidate()
    routing.invalidate_cache(app_id)
    audit_log(user.username, "route_add", f"app '{a.name}' (id={app_id}): {r.hostname}{r.path_prefix} {r.kind}")
    return _route_json(r)


@router.put("/apps/{app_id}/routes/{route_id}")
async def put_route(app_id: int, route_id: int, req: RouteReq, request: Request, session: Session = Depends(get_session),
                    user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    r = session.get(Route, route_id)
    if not r or r.app_id != app_id:
        raise HTTPException(404)
    fields = _clean_route(session, req, app_id, route_id, request, user)
    await _check_route_port(session, _main(session, app_id), fields["kind"], request)
    for k, v in fields.items():
        setattr(r, k, v)
    session.add(r)
    session.commit()
    proxy.invalidate()
    routing.invalidate_cache(app_id)
    audit_log(user.username, "route_change", f"app id={app_id}: {r.hostname}{r.path_prefix}")
    return _route_json(r)


@router.delete("/apps/{app_id}/routes/{route_id}")
def del_route(app_id: int, route_id: int, session: Session = Depends(get_session),
              user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    r = session.get(Route, route_id)
    if not r or r.app_id != app_id:
        raise HTTPException(404)
    session.delete(r)
    session.commit()
    proxy.invalidate()
    routing.invalidate_cache(app_id)
    audit_log(user.username, "route_delete", f"app id={app_id}: {r.hostname}{r.path_prefix}")
    return {"ok": True}


@router.get("/apps/{app_id}/access-log")
def access_log(app_id: int, lines: int = Query(200, ge=1, le=5000), session: Session = Depends(get_session),
               user: User = Depends(get_current_user)):
    _main(session, app_id)
    f = routing.access_log_file(app_id)
    if not f.exists():
        return {"lines": []}
    with open(f, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 1024 * 1024))
        data = fh.read().decode("utf-8", errors="replace").splitlines()
    return {"lines": data[-lines:]}


@router.get("/apps/{app_id}/traffic")
def traffic(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _main(session, app_id)
    st = routing.stats.get(app_id)
    if not st:
        return {"requests": {}, "bytes": 0, "avg_ms": None}
    n = sum(st["requests"].values())
    return {"requests": dict(st["requests"]), "bytes": st["bytes"], "avg_ms": (st["sum"] / n * 1000) if n else None,
            "upstreams": [{"app_id": a, "port": p} for a, p in (routing.target(app_id).ports if routing.target(app_id) else [])]}


# ---------------------------------------------------------------------------
# Port forwards (admin: they open ports on the host)
# ---------------------------------------------------------------------------

class ForwardReq(BaseModel):
    protocol: str = "tcp"
    listen_port: int
    target_port: Optional[int] = None
    listen_host: str = "0.0.0.0"
    enabled: bool = True


@router.get("/apps/{app_id}/forwards")
def list_forwards(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _main(session, app_id)
    return [{**f.model_dump(), **forwards.state(f.id)} for f in
            session.exec(select(PortForward).where(PortForward.app_id == app_id)).all()]


@router.post("/apps/{app_id}/forwards")
async def add_forward(app_id: int, req: ForwardReq, session: Session = Depends(get_session),
                      admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    import ipaddress
    from venvy.config import venvy_port
    a = _main(session, app_id)
    if req.protocol not in ("tcp", "udp"):
        raise HTTPException(400, "protocol must be tcp or udp.")
    target = req.target_port or a.port
    if not target or not 1 <= target <= 65535 or not 1 <= req.listen_port <= 65535:
        raise HTTPException(400, "Give the port to listen on (and the app's port, if it has none yet).")
    if req.listen_port == venvy_port():
        raise HTTPException(400, "That is Venvy's own port.")
    try:
        ipaddress.ip_address(req.listen_host)
    except ValueError:
        raise HTTPException(400, "listen_host must be an IP address (0.0.0.0 for all).")
    if session.exec(select(PortForward).where(PortForward.listen_port == req.listen_port)).first():
        raise HTTPException(409, f"Port {req.listen_port} is already forwarded.")
    f = PortForward(app_id=app_id, protocol=req.protocol, listen_host=req.listen_host, listen_port=req.listen_port,
                    target_port=target, enabled=req.enabled)
    session.add(f)
    session.commit()
    session.refresh(f)
    await forwards.sync()
    audit_log(admin.username, "forward_add", f"app '{a.name}' (id={app_id}): {req.protocol} {req.listen_port} -> {target}")
    return {**f.model_dump(), **forwards.state(f.id)}


@router.delete("/apps/{app_id}/forwards/{fid}")
async def del_forward(app_id: int, fid: int, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                      _rl: None = Depends(rate_limit_mutations)):
    f = session.get(PortForward, fid)
    if not f or f.app_id != app_id:
        raise HTTPException(404)
    session.delete(f)
    session.commit()
    await forwards.sync()
    audit_log(admin.username, "forward_delete", f"app id={app_id}: {f.protocol} {f.listen_port}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------

class VolumeReq(BaseModel):
    name: str


@router.get("/volumes")
def list_volumes(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    out = []
    for v in session.exec(select(Volume)).all():
        apps = [{"app_id": av.app_id, "read_only": av.read_only}
                for av in session.exec(select(AppVolume).where(AppVolume.volume_id == v.id)).all()]
        out.append({"id": v.id, "name": v.name, "path": str(volumes.path_of(v.name)), "env": volumes.env_name(v.name),
                    "apps": apps})
    return out


@router.post("/volumes")
def add_volume(req: VolumeReq, session: Session = Depends(get_session), admin: User = Depends(check_admin),
               _rl: None = Depends(rate_limit_mutations)):
    name = req.name.strip().lower()
    if not volumes.NAME_RE.fullmatch(name):
        raise HTTPException(400, "A volume name is lowercase letters, digits and -.")
    if session.exec(select(Volume).where(Volume.name == name)).first():
        raise HTTPException(409, "There is already a volume with that name.")
    v = Volume(name=name)
    session.add(v)
    session.commit()
    session.refresh(v)
    volumes.create_dir(name)
    audit_log(admin.username, "volume_add", name)
    return {"id": v.id, "name": v.name}


@router.delete("/volumes/{vid}")
def del_volume(vid: int, purge: bool = False, session: Session = Depends(get_session),
               admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    v = session.get(Volume, vid)
    if not v:
        raise HTTPException(404)
    if session.exec(select(AppVolume).where(AppVolume.volume_id == vid)).first():
        raise HTTPException(409, "Detach the volume from its apps first.")
    session.delete(v)
    session.commit()
    if purge:
        volumes.remove_dir(v.name)
    audit_log(admin.username, "volume_delete", f"{v.name}{' (files deleted)' if purge else ''}")
    return {"ok": True}


class AttachReq(BaseModel):
    read_only: bool = False


@router.post("/apps/{app_id}/volumes/{vid}")
def attach_volume(app_id: int, vid: int, req: AttachReq = AttachReq(), session: Session = Depends(get_session),
                  admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    v = session.get(Volume, vid)
    if not v:
        raise HTTPException(404)
    row = session.get(AppVolume, (app_id, vid)) or AppVolume(app_id=app_id, volume_id=vid)
    row.read_only = req.read_only
    session.add(row)
    session.commit()
    try:
        volumes.apply_acl(v)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    audit_log(admin.username, "volume_attach", f"{v.name} -> app '{a.name}'{' (read-only)' if req.read_only else ''}")
    return {"ok": True, "restart_needed": a.status == "running"}


@router.delete("/apps/{app_id}/volumes/{vid}")
def detach_volume(app_id: int, vid: int, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                  _rl: None = Depends(rate_limit_mutations)):
    row = session.get(AppVolume, (app_id, vid))
    if not row:
        raise HTTPException(404)
    session.delete(row)
    session.commit()
    v = session.get(Volume, vid)
    if v:
        with contextlib.suppress(RuntimeError):
            volumes.apply_acl(v)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Managed services
# ---------------------------------------------------------------------------

class ServiceReq(BaseModel):
    template: str
    name: str


@router.get("/services/templates")
def service_templates(user: User = Depends(get_current_user)):
    return {"templates": services.describe(), "docker": not runtimes.missing_tools("docker")}


@router.post("/services")
async def create_service(req: ServiceReq, session: Session = Depends(get_session), user: User = Depends(require_operator),
                         _rl: None = Depends(rate_limit_mutations)):
    """Create a database / cache / queue and start it. Answers the new app."""
    from venvy.isolation import _ensure_app_user
    from venvy.process import find_free_port, start_app_process_logic
    if req.template not in services.TEMPLATES:
        raise HTTPException(400, "Unknown service. Pick one of " + ", ".join(services.TEMPLATES))
    missing = runtimes.tools_error("docker")
    if missing:
        raise HTTPException(400, missing)
    name = _clean_app_name(req.name)
    f = services.new_service_fields(req.template)
    slug = sanitize_slug(name)
    user_dir = APPS_BASE_DIR / str(user.id)
    user_dir.mkdir(exist_ok=True)
    d = user_dir / slug
    if d.exists():
        slug = f"{slug}-{int(time.time())}-{secrets.token_hex(3)}"
        d = user_dir / slug
    d.mkdir(mode=0o750, parents=True)
    env = f.pop("env")
    row = App(user_id=user.id, name=name, slug=slug, app_dir=str(d), venv_path=str(d / "venv"),
              env_json=_encrypt_secret(json.dumps(env)), status="stopped",
              port=await asyncio.to_thread(find_free_port), **f)
    session.add(row)
    session.commit()
    session.refresh(row)
    if user.role != "admin" and not user.all_apps:
        session.add(AppAccess(user_id=user.id, app_id=row.id))
        session.commit()
    await asyncio.to_thread(_ensure_app_user, row)
    audit_log(user.username, "service_create", f"{req.template} '{name}' (id={row.id})")
    with contextlib.suppress(Exception):
        await start_app_process_logic(row.id, session)
    session.refresh(row)
    return _app_safe(row)


class BindReq(BaseModel):
    service_app_id: int
    prefix: str = ""


@router.get("/apps/{app_id}/services")
def list_bindings(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _main(session, app_id)
    out = []
    for b in session.exec(select(ServiceBinding).where(ServiceBinding.app_id == app_id)).all():
        svc = session.get(App, b.service_app_id)
        env = services.connection_env(svc, b.prefix) if svc else {}
        out.append({"id": b.id, "service_app_id": b.service_app_id, "service": svc.name if svc else None,
                    "template": svc.service_template if svc else None, "prefix": b.prefix,
                    "variables": sorted(env)})
    return out


@router.post("/apps/{app_id}/services")
def bind_service(app_id: int, req: BindReq, session: Session = Depends(get_session), user: User = Depends(require_operator),
                 _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    svc = session.get(App, req.service_app_id)
    if not svc or not svc.service_template or not has_app_access(session, user, svc.id):
        raise HTTPException(404, "No such service.")
    if svc.id == app_id:
        raise HTTPException(400, "A service can't use itself.")
    prefix = req.prefix.strip().upper()
    if prefix and not services.PREFIX_RE.fullmatch(prefix):
        raise HTTPException(400, "The prefix is upper-case letters, digits and _ (like CACHE).")
    if session.exec(select(ServiceBinding).where(ServiceBinding.app_id == app_id,
                                                 ServiceBinding.service_app_id == svc.id)).first():
        raise HTTPException(409, "The app already uses this service.")
    session.add(ServiceBinding(app_id=app_id, service_app_id=svc.id, prefix=prefix))
    session.commit()
    audit_log(user.username, "service_bind", f"app '{a.name}' uses '{svc.name}'")
    return {"ok": True, "restart_needed": a.status == "running"}


@router.delete("/apps/{app_id}/services/{bid}")
def unbind_service(app_id: int, bid: int, session: Session = Depends(get_session), user: User = Depends(require_operator),
                   _rl: None = Depends(rate_limit_mutations)):
    b = session.get(ServiceBinding, bid)
    if not b or b.app_id != app_id:
        raise HTTPException(404)
    session.delete(b)
    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Toolchains
# ---------------------------------------------------------------------------

class ToolchainReq(BaseModel):
    kind: str
    version: str = "latest"


@router.get("/toolchains")
def list_toolchains(user: User = Depends(get_current_user)):
    return {"dir": str(toolchains.TOOLCHAINS_DIR), "auto_install": toolchains.AUTO_INSTALL, "kinds": toolchains.describe()}


@router.post("/toolchains")
async def install_toolchain(req: ToolchainReq, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    if req.kind not in toolchains.KINDS:
        raise HTTPException(400, "kind must be one of " + ", ".join(toolchains.KINDS))
    try:
        toolchains.clean_version(req.kind, req.version)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit_log(admin.username, "toolchain_install", f"{req.kind} {req.version}")

    async def job() -> dict:
        lines: List[str] = []
        try:
            v = await toolchains.install(req.kind, req.version, lines.append)
        except toolchains.ToolchainError as exc:
            raise HTTPException(500, str(exc))
        return {"kind": req.kind, "version": v, "log": lines[-50:]}
    return _start_job(admin, "toolchain_install", job, min_role="admin")


@router.delete("/toolchains/{kind}/{version}")
def remove_toolchain(kind: str, version: str, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    try:
        ok = toolchains.remove(kind, version)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not ok:
        raise HTTPException(404)
    audit_log(admin.username, "toolchain_remove", f"{kind} {version}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Secrets back-ends, log shipping, telemetry, status page (admin settings)
# ---------------------------------------------------------------------------

class SecretsCfg(BaseModel):
    vault_addr: Optional[str] = None
    vault_token: Optional[str] = None       # "" clears; None keeps
    vault_namespace: Optional[str] = None
    sops_age_key: Optional[str] = None      # "" clears; None keeps


@router.get("/system/secrets")
def get_secrets_cfg(admin: User = Depends(check_admin)):
    v = kv.get_json(secrets_ext.VAULT_KEY, {}, secret=True) or {}
    return {"vault_addr": v.get("addr", ""), "vault_namespace": v.get("namespace", ""),
            "vault_token_set": bool(v.get("token")), "sops_age_key_set": bool(kv.get(secrets_ext.SOPS_KEY)),
            "sops_installed": bool(shutil.which("sops"))}


@router.put("/system/secrets")
def put_secrets_cfg(req: SecretsCfg, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    from venvy.notify import _check_safe_outbound_url
    v = kv.get_json(secrets_ext.VAULT_KEY, {}, secret=True) or {}
    if req.vault_addr is not None:
        addr = req.vault_addr.strip().rstrip("/")
        if addr:
            if not addr.startswith(("https://", "http://")):
                raise HTTPException(400, "The Vault address must be http(s)://")
            with contextlib.suppress(Exception):
                _check_safe_outbound_url(addr, "vault_addr")
        if addr != v.get("addr"):
            v.pop("token", None) if req.vault_token is None else None     # a token goes with its server
        v["addr"] = addr
    if req.vault_token is not None:
        v["token"] = req.vault_token.strip()
    if req.vault_namespace is not None:
        v["namespace"] = req.vault_namespace.strip()
    kv.put_json(secrets_ext.VAULT_KEY, v, secret=True)
    if req.sops_age_key is not None:
        key = req.sops_age_key.strip()
        if key and not key.startswith("AGE-SECRET-KEY-"):
            raise HTTPException(400, "That is not an age secret key (AGE-SECRET-KEY-…).")
        kv.put(secrets_ext.SOPS_KEY, _encrypt_secret(key) if key else "")
    audit_log(admin.username, "secrets_backend_change", "vault/sops settings")
    return get_secrets_cfg(admin)


@router.get("/system/log-targets")
def get_log_targets(admin: User = Depends(check_admin)):
    return logship.masked()


@router.put("/system/log-targets")
def put_log_targets(targets: List[dict], admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    old = logship.load_targets()
    out = []
    for i, t in enumerate(targets[:20]):
        keep = old[t["id"]] if isinstance(t.get("id"), int) and 0 <= t["id"] < len(old) else {}
        merged = {**keep, **{k: v for k, v in t.items() if v not in (None, "") or k == "enabled"}}
        try:
            out.append(logship.clean_target(merged))
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"Target {i + 1}: {exc}")
    logship.save_targets(out)
    audit_log(admin.username, "log_targets_change", f"{len(out)} target(s)")
    return logship.masked()


class TelemetryCfg(BaseModel):
    otlp_url: Optional[str] = None
    otlp_headers: Optional[Dict[str, str]] = None
    heartbeat_url: Optional[str] = None


@router.get("/system/telemetry")
def get_telemetry(admin: User = Depends(check_admin)):
    o = kv.get_json(telemetry.OTLP_KEY, {}, secret=True) or {}
    h = kv.get_json(telemetry.HEARTBEAT_KEY, {}, secret=True) or {}
    return {"otlp_url": o.get("url", ""), "otlp_headers": sorted((o.get("headers") or {}).keys()),
            "heartbeat_url": bool(h.get("url")), "state": telemetry.states(), "firewall": firewall.status()}


@router.put("/system/telemetry")
def put_telemetry(req: TelemetryCfg, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    from venvy.notify import _check_safe_outbound_url
    if req.otlp_url is not None or req.otlp_headers is not None:
        o = kv.get_json(telemetry.OTLP_KEY, {}, secret=True) or {}
        if req.otlp_url is not None:
            u = req.otlp_url.strip()
            if u:
                if not u.startswith(("https://", "http://")):
                    raise HTTPException(400, "The OTLP URL must be http(s)://")
                _check_safe_outbound_url(u, "otlp_url")
            o["url"] = u
        if req.otlp_headers is not None:
            for k, v in req.otlp_headers.items():
                if not _HEADER_RE.fullmatch(k) or any(c in v for c in "\r\n\0"):
                    raise HTTPException(400, f"The header {k!r} can't be set.")
            o["headers"] = req.otlp_headers
        kv.put_json(telemetry.OTLP_KEY, o, secret=True)
    if req.heartbeat_url is not None:
        u = req.heartbeat_url.strip()
        if u:
            if not u.startswith(("https://", "http://")):
                raise HTTPException(400, "The heartbeat URL must be http(s)://")
            _check_safe_outbound_url(u, "heartbeat_url")
        kv.put_json(telemetry.HEARTBEAT_KEY, {"url": u}, secret=True)
    audit_log(admin.username, "telemetry_change", "otlp/heartbeat settings")
    return get_telemetry(admin)


class MonitorReq(BaseModel):
    name: str
    target: str
    interval: int = 60
    timeout: int = 10
    expect_status: int = 0
    enabled: bool = True
    public: bool = False


def _mon_json(m: UrlMonitor) -> dict:
    d = m.model_dump()
    d["last_checked"] = m.last_checked.isoformat() if m.last_checked else None
    d["uptime_24h"] = telemetry.uptime(f"mon:{m.id}", 24)
    d["uptime_30d"] = telemetry.uptime(f"mon:{m.id}", 24 * 30)
    return d


def _clean_mon(req: MonitorReq, user: User) -> dict:
    name = req.name.strip()
    if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
        raise HTTPException(400, "A monitor needs a name (up to 60 characters).")
    try:
        target = telemetry.clean_target(req.target, local_ok=user.role == "admin")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not 10 <= req.interval <= 86400 or not 1 <= req.timeout <= 120 or not 0 <= req.expect_status <= 599:
        raise HTTPException(400, "interval 10-86400 s, timeout 1-120 s, expect_status 0-599.")
    return dict(name=name, target=target, interval=req.interval, timeout=req.timeout,
                expect_status=req.expect_status, enabled=req.enabled, public=req.public)


@router.get("/monitors")
def list_monitors(session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    return [_mon_json(m) for m in session.exec(select(UrlMonitor)).all()]


@router.post("/monitors")
def add_monitor(req: MonitorReq, session: Session = Depends(get_session), user: User = Depends(require_operator),
                _rl: None = Depends(rate_limit_mutations)):
    if len(session.exec(select(UrlMonitor)).all()) >= 200:
        raise HTTPException(400, "200 monitors at most.")
    m = UrlMonitor(**_clean_mon(req, user))
    session.add(m)
    session.commit()
    session.refresh(m)
    audit_log(user.username, "monitor_add", f"{m.name} {m.target}")
    return _mon_json(m)


@router.put("/monitors/{mid}")
def put_monitor(mid: int, req: MonitorReq, session: Session = Depends(get_session), user: User = Depends(require_operator),
                _rl: None = Depends(rate_limit_mutations)):
    m = session.get(UrlMonitor, mid)
    if not m:
        raise HTTPException(404)
    for k, v in _clean_mon(req, user).items():
        setattr(m, k, v)
    session.add(m)
    session.commit()
    return _mon_json(m)


@router.delete("/monitors/{mid}")
def del_monitor(mid: int, session: Session = Depends(get_session), user: User = Depends(require_operator),
                _rl: None = Depends(rate_limit_mutations)):
    m = session.get(UrlMonitor, mid)
    if not m:
        raise HTTPException(404)
    session.delete(m)
    session.commit()
    audit_log(user.username, "monitor_delete", m.name)
    return {"ok": True}


@router.get("/apps/{app_id}/uptime")
def app_uptime(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _app(session, app_id)
    return {"uptime_24h": telemetry.uptime(f"app:{app_id}", 24), "uptime_7d": telemetry.uptime(f"app:{app_id}", 24 * 7),
            "uptime_30d": telemetry.uptime(f"app:{app_id}", 24 * 30)}


class StatusPageCfg(BaseModel):
    enabled: bool = False
    title: str = "Status"
    apps: List[int] = []


@router.get("/system/status-page")
def get_status_page(admin: User = Depends(check_admin)):
    return kv.get_json(telemetry.STATUS_KEY, {"enabled": False, "title": "Status", "apps": []})


@router.put("/system/status-page")
def put_status_page(req: StatusPageCfg, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    title = req.title.strip()[:80] or "Status"
    kv.put_json(telemetry.STATUS_KEY, {"enabled": req.enabled, "title": title, "apps": [int(a) for a in req.apps][:200]})
    audit_log(admin.username, "status_page_change", "on" if req.enabled else "off")
    return get_status_page(admin)


@router.get("/system/firewall")
async def firewall_status(admin: User = Depends(check_admin)):
    st = await firewall.apply()
    return {**st, "ruleset": await asyncio.to_thread(firewall.ruleset) if st.get("active") else ""}


# ---------------------------------------------------------------------------
# Edge: Caddy, Tailscale, ngrok
# ---------------------------------------------------------------------------

class CaddyReq(BaseModel):
    enabled: bool
    email: str = ""
    venvy_hosts: List[str] = []


@router.get("/system/edge")
async def edge_status(admin: User = Depends(check_admin)):
    return {"caddy": await asyncio.to_thread(edge.caddy_status), "tailscale": await edge.tailscale_status(),
            "ngrok": edge.ngrok_status()}


@router.put("/system/edge/caddy")
async def put_caddy(req: CaddyReq, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    try:
        st = await edge.apply_caddy(req.model_dump())
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    audit_log(admin.username, "edge_caddy_change", "on" if req.enabled else "off")
    return st


class NgrokTokenReq(BaseModel):
    token: str


@router.put("/system/edge/ngrok")
def put_ngrok(req: NgrokTokenReq, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    t = req.token.strip()
    if t and (len(t) > 200 or not re.fullmatch(r"[A-Za-z0-9_\-]+", t)):
        raise HTTPException(400, "That doesn't look like an ngrok authtoken.")
    kv.put_json(edge.NGROK_KEY, {"token": t}, secret=True)
    audit_log(admin.username, "edge_ngrok_token", "set" if t else "cleared")
    return edge.ngrok_status()


class PublishReq(BaseModel):
    provider: str                     # tailscale | ngrok
    mode: str = "serve"               # tailscale: serve | funnel
    https_port: int = 443
    domain: str = ""                  # ngrok: the reserved / static domain


@router.post("/apps/{app_id}/publish")
async def publish(app_id: int, req: PublishReq, session: Session = Depends(get_session),
                  admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    if req.provider == "tailscale":
        if not a.port:
            raise HTTPException(400, "Start the app first, so it has a port.")
        try:
            url = await edge.tailscale_publish(app_id, a.port, req.mode, req.https_port)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
    elif req.provider == "ngrok":
        dom = req.domain.strip().lower()
        if not own_hosts.valid_hostname(dom):
            raise HTTPException(400, "Give the ngrok domain (e.g. myapp.ngrok.app).")
        if not edge.ngrok_token():
            raise HTTPException(400, "Set the ngrok authtoken under System → Edge first.")
        if any(o != app_id for o in routing.hostname_apps(session, dom)):
            raise HTTPException(409, f"{dom} is already used by another app.")
        if not session.exec(select(Route).where(Route.hostname == dom)).first() and a.domain != dom:
            session.add(Route(app_id=app_id, hostname=dom, path_prefix="/"))
            session.commit()
            proxy.invalidate()
        ng = edge.ngrok_apps()
        ng[str(app_id)] = dom
        kv.put_json(edge.NGROK_APPS_KEY, ng)
        url = f"https://{dom}"
    else:
        raise HTTPException(400, "provider must be tailscale or ngrok.")
    a.external_url = url
    session.add(a)
    session.commit()
    audit_log(admin.username, "app_publish", f"app '{a.name}' via {req.provider}: {url}")
    return {"url": url}


@router.delete("/apps/{app_id}/publish/{provider}")
async def unpublish(app_id: int, provider: str, session: Session = Depends(get_session),
                    admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    if provider == "tailscale":
        await edge.tailscale_unpublish(app_id)
    elif provider == "ngrok":
        ng = edge.ngrok_apps()
        dom = ng.pop(str(app_id), None)
        kv.put_json(edge.NGROK_APPS_KEY, ng)
        if dom:
            for r in session.exec(select(Route).where(Route.hostname == dom, Route.app_id == app_id)).all():
                session.delete(r)
            session.commit()
            proxy.invalidate()
    else:
        raise HTTPException(400, "provider must be tailscale or ngrok.")
    a.external_url = None
    session.add(a)
    session.commit()
    return {"ok": True}


@public.get("/_venvy/edge-ask")
def edge_ask(request: Request, domain: str = ""):
    """Caddy's on-demand TLS question: may it get a certificate for *domain*?
    Answered only to this host."""
    client = (request.client.host if request.client else "") or ""
    if client not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        raise HTTPException(403)
    # Caddy and cloudflared pass visitors on from loopback too: what they
    # forward carries their headers, Caddy's own question does not.
    if any(h in request.headers for h in ("x-forwarded-for", "cf-connecting-ip", "x-venvy-proxy-secret", "forwarded")):
        raise HTTPException(403)
    if edge.ask(domain):
        return PlainTextResponse("ok")
    raise HTTPException(404)


# ---------------------------------------------------------------------------
# Database replication
# ---------------------------------------------------------------------------

class ReplicationReq(BaseModel):
    minutes: int


@router.get("/system/replication")
def get_replication(admin: User = Depends(check_admin)):
    return replication.status()


@router.put("/system/replication")
def put_replication(req: ReplicationReq, admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    if not 0 <= req.minutes <= 1440:
        raise HTTPException(400, "minutes must be 0 (off) to 1440.")
    kv.put_json(replication.KEY, {"minutes": req.minutes})
    audit_log(admin.username, "replication_change", f"every {req.minutes} min" if req.minutes else "off")
    return replication.status()


@router.post("/system/replication/run")
async def run_replication(admin: User = Depends(check_admin), _rl: None = Depends(rate_limit_mutations)):
    try:
        where = await replication.replicate_once()
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "where": where}


# ---------------------------------------------------------------------------
# Other servers
# ---------------------------------------------------------------------------

class ServerReq(BaseModel):
    name: str
    url: str
    token: str


@router.get("/servers")
def list_servers(session: Session = Depends(get_session), admin: User = Depends(check_admin)):
    return [{"id": s.id, "name": s.name, "url": s.url, "last_status": s.last_status,
             "last_seen": s.last_seen.isoformat() if s.last_seen else None}
            for s in session.exec(select(RemoteServer)).all()]


@router.post("/servers")
async def add_server(req: ServerReq, session: Session = Depends(get_session), admin: User = Depends(check_admin),
                     _rl: None = Depends(rate_limit_mutations)):
    name = req.name.strip()
    if not federation.NAME_RE.fullmatch(name):
        raise HTTPException(400, "A server name is up to 40 letters, digits, spaces, . _ -")
    try:
        url = federation.clean_url(req.url)
        token = federation.encrypt_token(req.token.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if session.exec(select(RemoteServer).where(RemoteServer.name == name)).first():
        raise HTTPException(409, "There is already a server with that name.")
    srv = RemoteServer(name=name, url=url, token=token)
    session.add(srv)
    session.commit()
    session.refresh(srv)
    audit_log(admin.username, "server_add", f"{name} {url}")
    res = await federation.check(srv.id)
    return {"id": srv.id, "status": res["status"]}


@router.delete("/servers/{sid}")
def del_server(sid: int, session: Session = Depends(get_session), admin: User = Depends(check_admin),
               _rl: None = Depends(rate_limit_mutations)):
    srv = session.get(RemoteServer, sid)
    if not srv:
        raise HTTPException(404)
    session.delete(srv)
    session.commit()
    audit_log(admin.username, "server_delete", srv.name)
    return {"ok": True}


@router.get("/servers/apps")
async def server_apps(admin: User = Depends(check_admin)):
    return await federation.all_apps()


@router.post("/servers/{sid}/apps/{remote_app_id}/{action}")
async def server_app_action(sid: int, remote_app_id: int, action: str, admin: User = Depends(check_admin),
                            _rl: None = Depends(rate_limit_mutations)):
    try:
        res = await federation.control(sid, remote_app_id, action)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    audit_log(admin.username, "server_app_action", f"server {sid} app {remote_app_id}: {action}")
    return res


# ---------------------------------------------------------------------------
# App backups
# ---------------------------------------------------------------------------

@router.get("/apps/{app_id}/backups")
def list_app_backups(app_id: int, session: Session = Depends(get_session), user: User = Depends(require_operator)):
    _main(session, app_id)
    return appbackup.list_backups(app_id)


@router.post("/apps/{app_id}/backups")
async def make_app_backup(app_id: int, session: Session = Depends(get_session), user: User = Depends(require_operator),
                    _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    audit_log(user.username, "app_backup", f"app '{a.name}' (id={app_id})")

    async def job() -> dict:
        try:
            return await appbackup.create(app_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(500, str(exc))
    return _start_job(user, "app_backup", job, app_id=app_id, min_role="operator")


class AppRestoreReq(BaseModel):
    settings: bool = False


@router.post("/apps/{app_id}/backups/{name}/restore")
async def restore_app_backup(app_id: int, name: str, req: AppRestoreReq = AppRestoreReq(),
                       session: Session = Depends(get_session), user: User = Depends(require_operator),
                       _rl: None = Depends(rate_limit_mutations)):
    a = _main(session, app_id)
    try:
        appbackup.path_of(app_id, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    if deploy.is_running(app_id):
        raise HTTPException(409, "A deploy is running for this app.")
    audit_log(user.username, "app_restore", f"app '{a.name}' (id={app_id}) from {name}")

    async def job() -> dict:
        try:
            return await appbackup.restore(app_id, name, settings=req.settings)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(500, str(exc))
    return _start_job(user, "app_restore", job, app_id=app_id, min_role="operator")


@router.delete("/apps/{app_id}/backups/{name}")
def delete_app_backup(app_id: int, name: str, session: Session = Depends(get_session),
                      user: User = Depends(require_operator), _rl: None = Depends(rate_limit_mutations)):
    _main(session, app_id)
    try:
        appbackup.delete(app_id, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True}


@router.get("/apps/{app_id}/backups/{name}/download")
def download_app_backup(app_id: int, name: str, user: User = Depends(get_download_user)):
    from fastapi.responses import FileResponse
    if user.role not in ("admin", "operator"):
        raise HTTPException(403)
    try:
        p = appbackup.path_of(app_id, name)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return FileResponse(p, media_type="application/gzip", filename=name)


# ---------------------------------------------------------------------------
# Artifact deploys
# ---------------------------------------------------------------------------

_ARTIFACT_MAX = int(os.getenv("VENVY_ARTIFACT_MAX_MB", "2048")) * 1024 * 1024


@router.post("/apps/{app_id}/deploy/artifact")
async def deploy_artifact(app_id: int, file: UploadFile = File(...), start: bool = False,
                          session: Session = Depends(get_session), user: User = Depends(require_operator),
                          _rl: None = Depends(rate_limit_mutations)):
    """Deploy a .tar.gz of the app's files (built by CI, or by `ship.py deploy`): it
    replaces the app's folder as a whole — atomically, with the build in between —
    keeping venv/node_modules/target and data files (.env, *.db). Answers at once;
    follow GET /apps/{id}/deploy. A deploy already running queues this one."""
    from venvy.process import restore_in_progress
    a = _main(session, app_id)
    if a.status == "installing":
        raise HTTPException(409, "This app is still being set up.")
    if restore_in_progress():
        raise HTTPException(409, "A restore is in progress.")
    d = deploy.RELEASES_DIR / "incoming"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest = d / f"{app_id}-{time.time_ns()}.tar.gz"
    size = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > _ARTIFACT_MAX:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"The artifact is larger than {_ARTIFACT_MAX // (1024 * 1024)} MB.")
            out.write(chunk)
    import tarfile
    try:
        with tarfile.open(dest, "r:gz") as tf:
            for m in tf:
                n = m.name
                if n.startswith("/") or ".." in n.split("/") or not (m.isfile() or m.isdir() or m.issym()):
                    raise ValueError(f"unsafe entry {n!r}")
                if m.issym() and (m.linkname.startswith("/") or ".." in m.linkname.split("/")):
                    raise ValueError(f"unsafe link {n!r}")
    except (tarfile.TarError, ValueError, EOFError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"That is not a usable .tar.gz: {exc}")
    username = user.username
    a_id = a.id

    async def go() -> None:
        try:
            await deploy.run_deploy(app_id, username, "artifact", start_if_stopped=start, artifact=dest)
        except deploy.DeployError as exc:
            dest.unlink(missing_ok=True)
            logging.warning("Artifact deploy of app %s not started: %s", a_id, one_line(exc))
    from venvy.api.deploy import _start_background
    _start_background(go())
    audit_log(username, "app_deploy_artifact", f"app '{a.name}' (id={app_id}): {size} bytes")
    for _ in range(20):
        if deploy.is_running(app_id):
            break
        await asyncio.sleep(0.05)
    return {"ok": True, "queued": deploy.is_running(app_id) and app_id in deploy._pending}


@router.get("/apps/{app_id}/previews")
def list_previews(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _main(session, app_id)
    reveal = user.role in ("admin", "operator")
    return [_app_safe(p, reveal_secrets=reveal) for p in session.exec(select(App).where(App.preview_of == app_id)).all()]


# ---------------------------------------------------------------------------
# Public: metrics, status page
# ---------------------------------------------------------------------------

@public.get("/metrics")
def metrics(request: Request, session: Session = Depends(get_session)):
    """Prometheus metrics. Needs an API token (Authorization: Bearer …) or a session."""
    check_ip_allowlist(request)
    get_current_user(request, session)
    return PlainTextResponse(telemetry.prometheus(), media_type="text/plain; version=0.0.4")


@public.get("/status")
def status_page():
    data = telemetry.status_data()
    if data is None:
        raise HTTPException(404)
    return HTMLResponse(telemetry.status_html(data), headers={"Cache-Control": "no-cache"})


@public.get("/status.json")
def status_json():
    data = telemetry.status_data()
    if data is None:
        raise HTTPException(404)
    return JSONResponse(data, headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"})
