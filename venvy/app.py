"""The FastAPI application: lifespan (boot and shutdown of managed apps),
middleware, the API routers and the static routes that serve the UI."""
import asyncio
import contextlib
import logging
import os
import re
import secrets
import signal
import time
from typing import Dict, Set

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from sqlmodel import select, Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from venvy.config import (
    _ALLOWED_ORIGINS,
    ASSETS_DIR,
    WEB_DIR,
    SESSION_COOKIE_SECURE,
    utc_now,
    _VENVY_BOOT_TIME,
)
from venvy.backup import load_operation_state
from venvy.db import create_db_and_tables, engine
from venvy.models import App, SystemSetting
from venvy.process import (
    _active_app_pids,
    _app_pid_alive,
    _log_tasks,
    metrics_collector_task,
    _pid_alive,
    start_app_process_logic,
    _systemd_run_available,
    supervisor_task,
)
from venvy.startup import _check_python3_venv, _check_rust
from venvy.webui import render_index
from venvy.api import apps as api_apps
from venvy.api import auth as api_auth
from venvy.api import cloudflare as api_cloudflare
from venvy.api import files as api_files
from venvy.api import system as api_system
from venvy.api import terminal as api_terminal
from venvy.api import users as api_users


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    load_operation_state()

    await _check_python3_venv()
    await _check_rust()
    await asyncio.to_thread(_systemd_run_available)   # warm the (blocking) probe off the event loop

    with Session(engine) as session:
        _boot_reset_time = utc_now()
        for app_row in session.exec(select(App)).all():
            modified = False
            if not app_row.start_on_boot:
                app_row.last_manually_stopped_at = _boot_reset_time
                modified = True
            if app_row.status == "installing":
                # Venvy restarted mid-install; the venv may be incomplete.
                logging.warning(f"Install of '{app_row.name}' was interrupted — marking it as error.")
                app_row.status = "error"
                modified = True
            elif app_row.status in ("starting", "stopping"):
                # The task that would have finished this died with the old
                # process. Nothing else ever moves an app out of these states.
                app_row.status = "running" if app_row.pid else "stopped"
                modified = True

            if app_row.status == "running" and app_row.pid:
                if not _app_pid_alive(app_row):
                    logging.info(f"Clearing stale status for '{app_row.name}'")
                    app_row.status = "stopped"
                    app_row.pid = None
                    app_row.pid_start = None
                    app_row.last_manually_stopped_at = _boot_reset_time
                    modified = True
            
            if modified:
                session.add(app_row)
        session.commit()

    with Session(engine) as session:
        for app_row in session.exec(select(App).where(App.start_on_boot == True)).all():
            if app_row.status != "running":
                logging.info(f"Boot-starting '{app_row.name}'…")
                try:
                    await start_app_process_logic(app_row.id, session)
                except Exception as exc:
                    logging.error(f"Boot-start failed for '{app_row.name}': {exc}")

    supervisor_task_handle = asyncio.create_task(supervisor_task())
    metrics_task_handle = asyncio.create_task(metrics_collector_task())
    yield
    supervisor_task_handle.cancel()
    metrics_task_handle.cancel()
    try:
        await supervisor_task_handle
    except asyncio.CancelledError:
        pass

    logging.info("Venvy shutting down — stopping all managed apps…")
    
    pids_to_kill: Dict[int, Set[int]] = {}
    with Session(engine) as session:
        for app_row in session.exec(select(App).where(App.status == "running")).all():
            if _app_pid_alive(app_row):
                try:
                    pgid = os.getpgid(app_row.pid)
                    # SECURITY CRITICAL: Ensure we do not target Venvy's own PGID
                    if pgid != os.getpgid(0):
                        pids_to_kill.setdefault(pgid, set()).add(app_row.id)
                except OSError:
                    pass
                
        for aid, apid in _active_app_pids.items():
            if _pid_alive(apid):
                try:
                    pgid = os.getpgid(apid)
                    if pgid != os.getpgid(0):
                        pids_to_kill.setdefault(pgid, set()).add(aid)
                except OSError:
                    pass

    for pgid in pids_to_kill:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            pass

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not pids_to_kill:
            break
        still_alive = False
        for pgid in list(pids_to_kill.keys()):
            try:
                os.killpg(pgid, 0)
                still_alive = True
            except OSError:
                pids_to_kill.pop(pgid, None)
        if not still_alive:
            break
        await asyncio.sleep(0.25)

    with Session(engine) as session:
        for app_row in session.exec(select(App).where(App.status == "running")).all():
            app_row.status = "stopped"
            app_row.pid = None
            app_row.pid_start = None
            if app_row.last_stop_time is None:
                app_row.last_stop_time = utc_now()
            session.add(app_row)
        session.commit()

        for aid, apid in _active_app_pids.items():
            if _pid_alive(apid):
                try:
                    pgid = os.getpgid(apid)
                    if pgid != os.getpgid(0):
                        os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    pass

    for _log_task in list(_log_tasks.values()):
        if not _log_task.done():
            _log_task.cancel()
    _log_tasks.clear()


app = FastAPI(title="Venvy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS if _ALLOWED_ORIGINS else [],
    allow_origin_regex=None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add conservative security headers to every response."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' "
            "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
            "https://cdn.jsdelivr.net; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss: "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "worker-src 'self' blob:; "
            "manifest-src 'self' blob:; "
            "frame-ancestors 'none';",
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)

class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            csrf_cookie = request.cookies.get("csrf_token")
            csrf_header = request.headers.get("x-csrf-token")
            if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
        response = await call_next(request)
        if "csrf_token" not in request.cookies:
            token = secrets.token_urlsafe(32)
            response.set_cookie("csrf_token", token, httponly=False, samesite="lax", path="/", secure=SESSION_COOKIE_SECURE)
        return response

app.add_middleware(CSRFMiddleware)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app.include_router(api_auth.router)
app.include_router(api_users.router)
app.include_router(api_files.router)
app.include_router(api_cloudflare.router)
app.include_router(api_system.router)
app.include_router(api_apps.router)
app.include_router(api_terminal.router)


@app.get("/api/health")
def health_check():
    """Unauthenticated liveness check for external monitoring (uptime checks,
    Cloudflare active health checks, systemd watchdogs, etc). Deliberately
    outside api_router so it isn't subject to the IP allowlist or session
    auth — a monitor needs to be able to reach this even if it can't log in.
    Does a trivial DB round-trip so a wedged/corrupted database still shows
    as unhealthy rather than a false "ok"."""
    try:
        with Session(engine) as s:
            s.exec(select(SystemSetting).limit(1)).first()
        db_ok = True
    except Exception as e:
        logging.error(f"Health check DB probe failed: {e}")
        db_ok = False
    status_code = 200 if db_ok else 503
    return JSONResponse(
        {
            "ok": db_ok,
            "status": "healthy" if db_ok else "degraded",
            "uptime_seconds": int((utc_now() - _VENVY_BOOT_TIME).total_seconds()),
        },
        status_code=status_code,
    )


_VENDOR_FILE_RE = re.compile(r"\A[A-Za-z0-9_.\-]+\.js\Z")


@app.get("/vendor/{name}")
async def serve_vendor(name: str):
    """Third-party scripts shipped with Venvy (see vendor/) — served from
    here instead of a CDN, so nobody else can change them."""
    path = ASSETS_DIR / "vendor" / name
    if not _VENDOR_FILE_RE.match(name) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


class _RevalidatedStaticFiles(StaticFiles):
    """The UI's own CSS and JS. Always revalidated (ETag / Last-Modified make
    that cheap), so a browser never runs a stale script against a newer
    server after an update."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _RevalidatedStaticFiles(directory=WEB_DIR / "static", check_dir=False), name="static")


@app.get("/sw.js")
async def serve_service_worker():
    """The PWA service worker. Has to come from the site root to be allowed
    to control it, and must not be cached hard or updates never arrive."""
    path = WEB_DIR / "sw.js"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/")
def serve_spa():
    if not (WEB_DIR / "index.html").exists():
        return PlainTextResponse("Frontend not found.", status_code=404)
    # Assembled per request from web/index.html and web/partials/ (a few
    # dozen small local reads), so edits show up on reload without a restart.
    return HTMLResponse(render_index(WEB_DIR))
