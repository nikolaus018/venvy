"""Deploy routes: git settings, deploying, releases and rollback, push webhooks, duplicating an app."""
import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from venvy import deploy, runtimes
from venvy.apps import _app_safe, _clean_app_name, sanitize_slug
from venvy.audit import audit_log
from venvy.auth import (
    _resolve_client_ip, check_ip_allowlist, get_current_user, rate_limit_mutations, require_app_access, require_operator,
)
from venvy.config import APPS_BASE_DIR, utc_now
from venvy.crypto import _decrypt_secret, _encrypt_secret
from venvy.db import engine, get_session
from venvy.isolation import _chown_app_dir, _delete_app_user, _ensure_app_user
from venvy.jobs import _running_tasks, _start_job
from venvy.models import App, AppAccess, Release, User

router = APIRouter(
    prefix="/api", tags=["deploy"], dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)]
)
# Reached by git hosts, which have no login: the signature on the request is the credential.
hooks = APIRouter(prefix="/api/hooks", tags=["deploy"])

_hook_hits: dict = {}


def _app_or_404(session: Session, app_id: int) -> App:
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(404)
    return app


def _git_info(app: App, reveal: bool) -> dict:
    secret = _decrypt_secret(app.webhook_secret) if app.webhook_secret else ""
    return {
        "url": app.git_url or "", "branch": app.git_branch or "main",
        "has_token": bool(app.git_auth), "build_command": app.build_command or "",
        "auto_deploy": app.auto_deploy, "public_key": deploy.public_key(app.id),
        "ssh": bool(app.git_url and deploy.is_ssh_url(app.git_url)),
        "webhook_path": f"/api/hooks/deploy/{app.id}", "has_webhook_secret": bool(secret),
        "webhook_secret": secret if reveal else None,
    }


@router.get("/apps/{app_id}/deploy")
def get_deploy(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    app = _app_or_404(session, app_id)
    return {
        "git": _git_info(app, reveal=user.role in ("admin", "operator")),
        "state": deploy.state(app_id),
        "last": {"at": app.last_deploy_at.isoformat() if app.last_deploy_at else None,
                 "status": app.last_deploy_status, "commit": app.last_deploy_commit},
        "releases": deploy.list_releases(app_id),
    }


class GitReq(BaseModel):
    git_url: Optional[str] = None
    git_branch: Optional[str] = None
    build_command: Optional[str] = None
    auto_deploy: Optional[bool] = None
    token: Optional[str] = None             # "" removes the stored token


@router.put("/apps/{app_id}/git")
def put_git(
    app_id: int, req: GitReq,
    session: Session = Depends(get_session), user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app = _app_or_404(session, app_id)
    try:
        if req.git_url is not None:
            app.git_url = deploy.clean_git_url(req.git_url) or None
        if req.git_branch is not None:
            app.git_branch = deploy.clean_branch(req.git_branch)
        if req.build_command is not None:
            app.build_command = deploy.clean_build_command(req.build_command)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if req.token is not None:
        tok = req.token.strip()
        if tok and (len(tok) > 512 or any(c in tok for c in " \t\r\n\0")):
            raise HTTPException(400, "That does not look like an access token.")
        if tok and not (app.git_url or "").startswith("https://"):
            raise HTTPException(400, "An access token is for https:// repositories; use the deploy key for ssh.")
        app.git_auth = _encrypt_secret(json.dumps({"token": tok})) if tok else None
    if req.auto_deploy is not None:
        if req.auto_deploy and not app.webhook_secret:
            app.webhook_secret = _encrypt_secret(deploy.new_webhook_secret())
        app.auto_deploy = req.auto_deploy
    app.updated_at = utc_now()
    session.add(app)
    session.commit()
    audit_log(user.username, "app_git_settings", f"app '{app.name}' (id={app_id}): {app.git_url or 'no repository'} "
                                                 f"branch={app.git_branch} build={'yes' if app.build_command else 'no'}")
    return _git_info(app, reveal=True)


@router.post("/apps/{app_id}/git/key")
async def make_deploy_key(app_id: int, session: Session = Depends(get_session), user: User = Depends(require_operator),
                          _rl: None = Depends(rate_limit_mutations)):
    """Generate an SSH key for this app (replacing any earlier one) and return the public half,
    to be added as a read-only deploy key at the git host."""
    app = _app_or_404(session, app_id)
    try:
        pub = await deploy.generate_deploy_key(app)
    except deploy.DeployError as exc:
        raise HTTPException(500, str(exc))
    audit_log(user.username, "app_deploy_key", f"app '{app.name}' (id={app_id})")
    return {"public_key": pub}


@router.post("/apps/{app_id}/git/webhook-secret")
def new_webhook_secret(app_id: int, session: Session = Depends(get_session), user: User = Depends(require_operator),
                       _rl: None = Depends(rate_limit_mutations)):
    app = _app_or_404(session, app_id)
    secret = deploy.new_webhook_secret()
    app.webhook_secret = _encrypt_secret(secret)
    session.add(app)
    session.commit()
    audit_log(user.username, "app_webhook_secret", f"app '{app.name}' (id={app_id})")
    return {"webhook_secret": secret}


def _start_background(coro) -> None:
    t = asyncio.ensure_future(coro)
    _running_tasks.add(t)
    t.add_done_callback(_running_tasks.discard)


class DeployReq(BaseModel):
    start: bool = False         # start the app afterwards even if it was stopped


@router.post("/apps/{app_id}/deploy")
async def start_deploy(
    app_id: int, req: DeployReq = DeployReq(),
    session: Session = Depends(get_session), user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Deploy now. Answers at once; follow GET /apps/{id}/deploy (`state`)."""
    app = _app_or_404(session, app_id)
    if app.status == "installing":
        raise HTTPException(409, "This app is still being set up.")
    if deploy.is_running(app_id):
        raise HTTPException(409, "A deploy is already running for this app.")
    if not app.git_url and not app.build_command:
        raise HTTPException(400, "Set a git repository or a build command first.")

    async def _go() -> None:
        try:
            await deploy.run_deploy(app_id, user.username, "manual", start_if_stopped=req.start)
        except deploy.DeployError as exc:
            logging.warning(f"Deploy of app {app_id} not started: {exc}")

    _start_background(_go())
    # Let the state appear, so the first poll already sees it running.
    for _ in range(20):
        if deploy.is_running(app_id):
            break
        await asyncio.sleep(0.05)
    return {"ok": True}


@router.get("/apps/{app_id}/releases")
def get_releases(app_id: int, session: Session = Depends(get_session), user: User = Depends(get_current_user)):
    _app_or_404(session, app_id)
    return deploy.list_releases(app_id)


@router.post("/apps/{app_id}/releases")
async def make_release(
    app_id: int, session: Session = Depends(get_session), user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Mark the app as it is now as a point to return to (a snapshot of its files, or the git commit)."""
    app = _app_or_404(session, app_id)

    async def _job() -> dict:
        if deploy.is_running(app_id):
            raise HTTPException(409, "A deploy is running for this app.")
        async with deploy._lock(app_id):
            now = await deploy.current_commit(app)
            if now:
                deploy._add_release(app_id, "manual", now[0], now[1], None, 0, user.username)
            else:
                log = deploy._Log(app_id, [])
                try:
                    name, size = await deploy._snapshot(app, log)
                except deploy.DeployError as exc:
                    raise HTTPException(500, str(exc))
                finally:
                    log.close()
                deploy._add_release(app_id, "manual", None, "Snapshot", name, size, user.username)
        audit_log(user.username, "app_release_create", f"app '{app.name}' (id={app_id})")
        return {"ok": True}

    return _start_job(user, "release", _job)


@router.post("/apps/{app_id}/releases/{release_id}/rollback")
async def rollback(
    app_id: int, release_id: int,
    session: Session = Depends(get_session), user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Go back to a release: the code, its dependencies, the build, a restart. Follow GET /apps/{id}/deploy."""
    app = _app_or_404(session, app_id)
    rel = session.get(Release, release_id)
    if not rel or rel.app_id != app_id:
        raise HTTPException(404, "No such release.")
    if not (rel.commit or rel.archive):
        raise HTTPException(400, "That release cannot be restored.")
    if deploy.is_running(app_id):
        raise HTTPException(409, "A deploy is already running for this app.")
    session.expunge(rel)

    async def _go() -> None:
        try:
            await deploy.run_deploy(app_id, user.username, "rollback", target=rel)
        except deploy.DeployError as exc:
            logging.warning(f"Rollback of app {app_id} not started: {exc}")

    _start_background(_go())
    for _ in range(20):
        if deploy.is_running(app_id):
            break
        await asyncio.sleep(0.05)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Push webhooks
# ---------------------------------------------------------------------------

@hooks.post("/deploy/{app_id}")
async def deploy_webhook(app_id: int, request: Request):
    """A push to the app's branch deploys it. Accepts GitHub, Gitea and Forgejo (HMAC-signed),
    GitLab (token header), or `Authorization: Bearer <secret>` from your own CI."""
    # Per client address, not per app id: ids are the caller's to make up.
    now = time.monotonic()
    ip = _resolve_client_ip(request)
    hits = [t for t in _hook_hits.get(ip, []) if now - t < 60]
    if len(hits) >= 60:
        raise HTTPException(429, "Too many requests.")
    hits.append(now)
    _hook_hits[ip] = hits
    if len(_hook_hits) > 5000:
        for k in [k for k, v in _hook_hits.items() if not v or now - v[-1] >= 60]:
            _hook_hits.pop(k, None)
    body = await request.body()
    with Session(engine) as s:
        app = s.get(App, app_id)
        secret = _decrypt_secret(app.webhook_secret) if app and app.webhook_secret else ""
        enabled = bool(app and app.auto_deploy)
        branch = app.git_branch if app else "main"
        name = app.name if app else ""
    # One answer for "no such app", "no secret" and "wrong secret".
    if not secret or not deploy.verify_webhook(request.headers, body, secret):
        raise HTTPException(401, "Invalid signature or token.")
    if request.headers.get("x-github-event") == "ping" or request.headers.get("x-gitea-event") == "ping":
        return {"ok": True, "pong": True}
    if not enabled:
        return {"ok": True, "deployed": False, "reason": "auto-deploy is off for this app"}
    pushed = deploy.webhook_branch(request.headers, body)
    if pushed is not None and pushed != branch:
        return {"ok": True, "deployed": False, "reason": f"push to {pushed}, not {branch}"}
    if deploy.is_running(app_id):
        return {"ok": True, "deployed": False, "reason": "a deploy is already running"}

    async def _go() -> None:
        try:
            await deploy.run_deploy(app_id, "webhook", "webhook")
        except deploy.DeployError as exc:
            logging.warning(f"Webhook deploy of app {app_id} not started: {exc}")

    _start_background(_go())
    audit_log("webhook", "app_webhook_deploy", f"app '{name}' (id={app_id})")
    return {"ok": True, "deployed": True}


# ---------------------------------------------------------------------------
# Duplicate
# ---------------------------------------------------------------------------

class DuplicateReq(BaseModel):
    name: Optional[str] = None


@router.post("/apps/{app_id}/duplicate")
async def duplicate_app(
    app_id: int, req: DuplicateReq = DuplicateReq(),
    session: Session = Depends(get_session), user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Copy an app: its files, settings and variables, with a fresh environment of its own.
    Not copied: its port, domain, public route, logs and run history. Answers {"job_id"} and "app_id"."""
    src = _app_or_404(session, app_id)
    if src.status == "installing":
        raise HTTPException(409, "This app is still being set up.")
    name = _clean_app_name(req.name or f"{src.name} copy")
    slug = sanitize_slug(name)
    user_dir = APPS_BASE_DIR / str(user.id)
    await asyncio.to_thread(user_dir.mkdir, exist_ok=True)
    new_dir = user_dir / slug
    if await asyncio.to_thread(new_dir.exists):
        slug = f"{slug}-{int(time.time())}"
        new_dir = user_dir / slug
    await asyncio.to_thread(new_dir.mkdir, parents=True, mode=0o750)
    src_snapshot = None
    new = App(
        user_id=user.id, name=name, slug=slug, runtime=src.runtime, app_dir=str(new_dir),
        venv_path=str(new_dir / "venv"), entrypoint=src.entrypoint, start_on_boot=False,
        restart_policy=src.restart_policy, port=None, status="installing", env_json=src.env_json,
        url_source_pref="local", memory_limit_mb=src.memory_limit_mb, cpu_limit_percent=src.cpu_limit_percent,
        healthcheck_path=src.healthcheck_path, health_interval=src.health_interval,
        health_failures=src.health_failures, health_restart=src.health_restart,
        git_url=src.git_url, git_branch=src.git_branch, git_auth=src.git_auth, build_command=src.build_command,
        runtime_version=src.runtime_version, disk_limit_mb=src.disk_limit_mb,
    )
    session.add(new)
    session.commit()
    session.refresh(new)
    new_id = new.id
    if user.role != "admin" and not user.all_apps:
        session.add(AppAccess(user_id=user.id, app_id=new_id))
        session.commit()
    audit_log(user.username, "app_duplicate", f"app '{src.name}' (id={app_id}) -> '{name}' (id={new_id})")

    async def _job() -> dict:
        try:
            from venvy.api.apps import _create_environment
            with Session(engine) as s:
                src_row, row = s.get(App, app_id), s.get(App, new_id)
            await asyncio.to_thread(_ensure_app_user, row)
            log = deploy._Log(new_id, [])
            try:
                archive, _ = await deploy._snapshot(src_row, log, RELEASES_TMP(new_id))
                await deploy._restore_snapshot(row, archive, log, RELEASES_TMP(new_id))
            finally:
                log.close()
                shutil.rmtree(RELEASES_TMP(new_id), ignore_errors=True)
            await asyncio.to_thread(_chown_app_dir, row)
            await _create_environment(row, row.runtime_version)
            with Session(engine) as s:
                row = s.get(App, new_id)
                row.status = "stopped"
                s.add(row)
                s.commit()
                s.refresh(row)
                return _app_safe(row)
        except BaseException:
            await asyncio.to_thread(shutil.rmtree, new_dir, ignore_errors=True)
            with Session(engine) as s:
                row = s.get(App, new_id)
                if row:
                    s.delete(row)
                    s.commit()
            await asyncio.to_thread(_delete_app_user, new_id)
            raise

    return {**_start_job(user, "app_duplicate", _job), "app_id": new_id}


def RELEASES_TMP(app_id: int) -> Path:
    return deploy.RELEASES_DIR / f"copy-{app_id}"
