"""Application CRUD and lifecycle control."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import aiofiles
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlmodel import Session, select

from venvy.config import settings
from venvy.core.audit import audit_log
from venvy.core.crypto import encrypt_secret
from venvy.core.paths import is_safe_path, is_safe_upload_name, sanitize_slug, validate_entrypoint
from venvy.core.process_compat import pid_alive, psutil
from venvy.core.timeutils import make_tz_aware, utc_now
from venvy.db.engine import get_session, session_scope
from venvy.db.models import App, CfRoute, User
from venvy.schemas import AppUpdateRequest
from venvy.security.dependencies import get_current_user, require_operator
from venvy.security.rate_limit import rate_limit_mutations
from venvy.serializers import serialize_app
from venvy.services import registry
from venvy.services.processes import AppStartError, start_app, stop_app

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps", tags=["apps"])

_VENV_CREATE_TIMEOUT = 120
_PIP_INSTALL_TIMEOUT = 600
#: Pause between stop and start during a restart, letting the OS release the port.
_RESTART_SETTLE_SECONDS = 0.1


# ── Listing ──────────────────────────────────────────────────────────────────


@router.get("")
def list_apps(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """List every app, reconciling obviously stale status rows first.

    Apps belong to the instance, not to the signed-in user: everyone
    authenticated sees everything. Read-only enforcement happens on the
    mutating routes, and secrets are redacted here for viewers.
    """
    apps = list(session.exec(select(App).order_by(App.name)).all())

    changed = False
    for app in apps:
        # Transient states belong to an in-flight request; leave them alone.
        if app.status in ("starting", "stopping"):
            continue
        if app.status == "running" and app.pid and not pid_alive(app.pid):
            app.status = "stopped"
            app.pid = None
            app.last_stop_time = utc_now()
            session.add(app)
            changed = True
        elif app.status == "stopped" and app.pid:
            app.pid = None
            session.add(app)
            changed = True
    if changed:
        session.commit()

    reveal = user.role in ("admin", "operator")
    return [serialize_app(app, reveal_secrets=reveal) for app in apps]


@router.get("/{app_id}/stats")
def app_stats(
    app_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Return a point-in-time CPU, memory and uptime reading for one app."""
    app = _get_app_or_404(session, app_id)
    stats = {"cpu": 0.0, "memory": 0.0, "uptime": 0.0}

    if app.pid and psutil is not None:
        try:
            process = psutil.Process(app.pid)
            stats["cpu"] = process.cpu_percent(interval=None)
            stats["memory"] = process.memory_info().rss / (1024 * 1024)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if app.last_start_time and app.status == "running":
        stats["uptime"] = (utc_now() - make_tz_aware(app.last_start_time)).total_seconds()
    return stats


# ── Creation ─────────────────────────────────────────────────────────────────


async def _write_uploads(files: list[UploadFile], app_dir: Path) -> None:
    """Write uploaded files into *app_dir*, skipping anything unsafe.

    Unsafe names are skipped rather than rejected so that one stray file in a
    dragged folder — a ``.DS_Store``, an editor swap file — does not fail the
    whole upload.
    """
    for upload in files:
        relative = upload.filename or "upload"
        if "\0" in relative or ".." in relative or relative.startswith("/"):
            continue
        if not is_safe_upload_name(Path(relative).name):
            continue

        destination = (app_dir / relative).resolve()
        if not is_safe_path(app_dir, destination):
            continue

        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        # Read one byte past the limit so an oversized file is detected without
        # ever holding more than the limit plus one byte in memory.
        data = await upload.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"File '{Path(relative).name}' exceeds the "
                f"{settings.max_upload_bytes // (1024 * 1024)} MB limit.",
            )
        async with aiofiles.open(destination, "wb") as handle:
            await handle.write(data)


async def _create_virtualenv(venv_path: Path) -> None:
    """Create a virtualenv, raising a 500 with actionable detail on failure."""
    process = await asyncio.create_subprocess_exec(
        "python3",
        "-m",
        "venv",
        str(venv_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=_VENV_CREATE_TIMEOUT)
    except TimeoutError as exc:
        process.kill()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Virtualenv creation timed out."
        ) from exc

    if process.returncode != 0:
        message = stderr.decode(errors="replace")
        hint = ""
        if "ensurepip" in message or "venv" in message.lower():
            hint = (
                f" Hint: python3-venv may not be installed — run "
                f"`sudo apt-get install -y python3.{sys.version_info.minor}-venv`."
            )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Virtualenv creation failed: {message}{hint}"
        )


async def _install_requirements(requirements: UploadFile, app_dir: Path, venv_path: Path) -> None:
    """Write requirements.txt and pip-install it into the new virtualenv."""
    requirements_path = app_dir / "requirements.txt"
    async with aiofiles.open(requirements_path, "wb") as handle:
        await handle.write(await requirements.read())

    pip = venv_path / "bin" / "pip"
    if not await asyncio.to_thread(pip.exists):
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "pip was not found in the new virtual environment.",
        )

    process = await asyncio.create_subprocess_exec(
        str(pip),
        "install",
        "--no-input",
        "-r",
        str(requirements_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=_PIP_INSTALL_TIMEOUT)
    except TimeoutError as exc:
        process.kill()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"pip install timed out after {_PIP_INSTALL_TIMEOUT // 60} minutes.",
        ) from exc

    if process.returncode != 0:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"pip install failed: {stderr.decode(errors='replace')}",
        )


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_app(
    name: str = Form(...),
    runtime: str = Form("python"),
    entrypoint: str = Form(""),
    start_on_boot: bool = Form(False),
    restart_policy: str = Form("never"),
    files: list[UploadFile] | None = File(None),
    requirements: UploadFile | None = File(None),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Create an app from uploaded sources, building its environment.

    Multipart rather than JSON because it carries the file payload. Any failure
    after the directory is created removes it again, so a failed upload never
    leaves a half-built app behind.
    """
    if runtime not in ("python", "rust"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "runtime must be one of: python, rust")
    if restart_policy not in ("never", "on-failure", "always"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "restart_policy must be one of: never, on-failure, always",
        )

    if runtime == "rust":
        # cargo decides the binary from Cargo.toml; main.rs is a stable label.
        entrypoint_clean = "main.rs"
    else:
        try:
            entrypoint_clean = validate_entrypoint(entrypoint)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    slug = sanitize_slug(name)
    owner_dir = settings.apps_dir / str(user.id)
    await asyncio.to_thread(owner_dir.mkdir, parents=True, exist_ok=True)

    app_dir = owner_dir / slug
    if await asyncio.to_thread(app_dir.exists):
        slug = f"{slug}-{int(time.time())}"
        app_dir = owner_dir / slug

    await asyncio.to_thread(app_dir.mkdir, parents=True, mode=0o750)
    venv_path = app_dir / "venv"

    try:
        await asyncio.to_thread((app_dir / "logs").mkdir, mode=0o750)
        if files:
            await _write_uploads(files, app_dir)
        if runtime == "python":
            await _create_virtualenv(venv_path)
            if requirements and requirements.filename:
                await _install_requirements(requirements, app_dir, venv_path)
    except Exception:
        await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
        raise

    app = App(
        user_id=user.id,
        name=name,
        slug=slug,
        runtime=runtime,
        app_dir=str(app_dir),
        venv_path=str(venv_path),
        entrypoint=entrypoint_clean,
        start_on_boot=start_on_boot,
        restart_policy=restart_policy,
        port=None,
    )
    session.add(app)
    session.commit()
    session.refresh(app)

    audit_log(user.username, "app_create", f"created '{app.name}' (id={app.id})")
    return serialize_app(app)


# ── Lifecycle ────────────────────────────────────────────────────────────────


def _get_app_or_404(session: Session, app_id: int) -> App:
    """Fetch an app or raise 404."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


def _mark_errored(app_id: int) -> None:
    """Flag an app as errored from a background task."""
    try:
        with session_scope() as session:
            app = session.get(App, app_id)
            if app:
                app.status = "error"
                session.add(app)
                session.commit()
    except Exception:
        logger.exception("Could not mark app %s as errored", app_id)


async def _background_start(app_id: int) -> None:
    """Start an app outside the request, so the HTTP call returns immediately."""
    try:
        with session_scope() as session:
            await start_app(app_id, session)
    except (AppStartError, ValueError, OSError) as exc:
        logger.error("Background start failed for app %s: %s", app_id, exc)
        _mark_errored(app_id)
    except Exception:
        logger.exception("Unexpected error starting app %s", app_id)
        _mark_errored(app_id)
    finally:
        registry.trigger_supervisor()


async def _background_stop(app_id: int) -> None:
    """Stop an app outside the request."""
    try:
        with session_scope() as session:
            await stop_app(app_id, session, manual=True)
    except Exception:
        logger.exception("Background stop failed for app %s", app_id)
    finally:
        registry.trigger_supervisor()


async def _background_restart(app_id: int) -> None:
    """Stop then start an app outside the request.

    The stop is *not* marked manual — a restart is not an instruction to keep
    the app down, and marking it so would stop the restart policy working
    afterwards.
    """
    try:
        with session_scope() as session:
            await stop_app(app_id, session, manual=False)

        await asyncio.sleep(_RESTART_SETTLE_SECONDS)

        # Clear a PID that outlived the stop, so start_app does not mistake the
        # dying process for a healthy one and decline to launch.
        with session_scope() as session:
            app = session.get(App, app_id)
            if app and app.pid and pid_alive(app.pid):
                app.pid = None
                app.status = "stopped"
                session.add(app)
                session.commit()

        with session_scope() as session:
            await start_app(app_id, session)
    except (AppStartError, ValueError, OSError) as exc:
        logger.error("Background restart failed for app %s: %s", app_id, exc)
        _mark_errored(app_id)
    except Exception:
        logger.exception("Unexpected error restarting app %s", app_id)
        _mark_errored(app_id)
    finally:
        registry.trigger_supervisor()


@router.post("/{app_id}/control")
async def control_app(
    app_id: int,
    action: str = Query(..., pattern="^(start|stop|restart)$"),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Start, stop or restart an app.

    Returns as soon as the work is queued. Starting an app can take a while —
    a venv to warm, a Rust crate to compile — so the transient ``starting`` /
    ``stopping`` status is written immediately and the UI polls for the result
    rather than holding a request open.

    Must stay ``async``: it schedules the real work with ``asyncio.create_task``,
    and a sync handler would run in FastAPI's thread pool where there is no
    running event loop to schedule onto.
    """
    app = _get_app_or_404(session, app_id)
    audit_log(user.username, f"app_{action}", f"'{app.name}' (id={app_id})")

    if action == "start":
        if app.status != "running":
            app.status = "starting"
        handler = _background_start
    elif action == "stop":
        if app.status != "stopped":
            app.status = "stopping"
        handler = _background_stop
    else:
        app.status = "stopping"
        handler = _background_restart

    session.add(app)
    session.commit()

    registry.track_task(asyncio.create_task(handler(app_id)))
    return {"status": app.status, "queued": True}


# ── Update and delete ────────────────────────────────────────────────────────


def _apply_port_changes(app: App, payload: AppUpdateRequest) -> None:
    """Apply port fields and recompute the app's effective port list.

    Pinned ports always win over detected ones; the app's primary ``port`` is
    the first entry of whichever list is in force.
    """
    if payload.port is not None:
        app.port = None if payload.port == 0 else payload.port

    if payload.manual_ports is not None:
        app.manual_ports = json.dumps(payload.manual_ports)
        if payload.manual_ports:
            app.port = payload.manual_ports[0]
        else:
            app.port = None

    try:
        manual = json.loads(app.manual_ports or "[]")
    except (ValueError, TypeError):
        manual = []
    try:
        detected = json.loads(app.detected_ports or "[]")
    except (ValueError, TypeError):
        detected = []

    combined = manual or detected
    app.detected_ports = json.dumps(combined)
    if not app.port and combined:
        app.port = combined[0]


@router.put("/{app_id}")
def update_app(
    app_id: int,
    payload: AppUpdateRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Update an app's configuration. Only supplied fields change.

    Configuration changes take effect on the app's next start; this never
    restarts a running app implicitly.
    """
    app = _get_app_or_404(session, app_id)

    if payload.name is not None:
        app.name = payload.name
    if payload.start_on_boot is not None:
        app.start_on_boot = payload.start_on_boot
    if payload.restart_policy is not None:
        app.restart_policy = payload.restart_policy
    if payload.url_source_pref is not None:
        app.url_source_pref = payload.url_source_pref

    _apply_port_changes(app, payload)

    if payload.env_vars is not None:
        app.env_json = encrypt_secret(json.dumps(payload.env_vars))
        audit_log(user.username, "app_env_change", f"'{app.name}' (id={app_id})")

    if payload.runtime is not None:
        app.runtime = payload.runtime
    if payload.entrypoint is not None:
        app.entrypoint = payload.entrypoint
    # Rust apps always report main.rs, whichever field triggered the change.
    if app.runtime == "rust":
        app.entrypoint = "main.rs"

    if payload.memory_limit_mb is not None:
        if payload.memory_limit_mb and payload.memory_limit_mb < 16:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "memory_limit_mb must be at least 16 (or 0 for unlimited).",
            )
        app.memory_limit_mb = payload.memory_limit_mb or None
    if payload.cpu_limit_percent is not None:
        app.cpu_limit_percent = payload.cpu_limit_percent or None

    app.updated_at = utc_now()
    session.add(app)
    session.commit()
    session.refresh(app)
    return serialize_app(app)


async def _cleanup_cloudflare_route(session: Session, app_id: int) -> None:
    """Remove an app's tunnel ingress rule and DNS record before deletion.

    Failures are logged and swallowed: a Cloudflare outage must not block
    deleting a local app, and the leftover route is visible and removable in
    the Cloudflare dashboard.
    """
    from venvy.services import cloudflare as cf

    route = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).first()
    if not route:
        return

    if route.cf_route_id and route.hostname:
        try:
            credentials = cf.get_credentials(session)
            ingress = await cf.get_tunnel_config(
                credentials.account_id, credentials.tunnel_id, credentials.api_token
            )
            await cf.put_tunnel_ingress(
                credentials.account_id,
                credentials.tunnel_id,
                credentials.api_token,
                cf.build_ingress(ingress, {route.hostname}),
            )
            zone_id = await cf.find_zone_id(
                route.hostname, credentials.account_id, credentials.api_token
            )
            if zone_id:
                await cf.delete_dns_cname(zone_id, route.hostname, credentials.api_token)
            logger.info("Cloudflare route %s cleaned up for deleted app %s", route.hostname, app_id)
        except Exception as exc:
            logger.warning("Cloudflare cleanup failed for app %s: %s", app_id, exc)

    session.delete(route)


@router.delete("/{app_id}")
async def delete_app(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Stop an app, unpublish it and delete its directory. Irreversible."""
    app = _get_app_or_404(session, app_id)
    app_name = app.name
    app_dir = Path(app.app_dir)

    registry.cancel_log_task(app_id)
    await stop_app(app_id, session)
    await _cleanup_cloudflare_route(session, app_id)

    if await asyncio.to_thread(app_dir.exists):
        await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)

    session.delete(app)
    session.commit()
    registry.unregister_pid(app_id)

    audit_log(user.username, "app_delete", f"deleted '{app_name}' (id={app_id})")
    return {"ok": True}
