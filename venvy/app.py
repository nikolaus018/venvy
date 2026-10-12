"""The FastAPI application: lifespan (boot and shutdown of managed apps),
middleware, the API routers and the static routes that serve the UI."""
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import signal
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from sqlmodel import select, Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from venvy.config import (
    _ALLOWED_ORIGINS,
    APPS_BASE_DIR,
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
    restart_app_process_logic,
    start_app_process_logic,
    stop_app_process_logic,
    _STOP_GRACE_SEC,
    _systemd_run_available,
    supervisor_task,
)
from venvy.startup import _check_python3_venv, _check_rust
from venvy.terminal import terminal_manager
from venvy.uploads import _SINGLE_REQUEST_UPLOAD_MAX, _UPLOAD_CHUNK_MAX
from venvy.webui import render_index
from venvy.api import apps as api_apps
from venvy.api import auth as api_auth
from venvy.api import cloudflare as api_cloudflare
from venvy.api import files as api_files
from venvy.api import system as api_system
from venvy.api import terminal as api_terminal
from venvy.api import users as api_users


def _rebase_app_paths(app_row: App) -> bool:
    """App rows store absolute paths. After a backup is restored onto a host
    whose data directory is somewhere else, they still name the old place;
    the files are here, under this server's apps/. Point the row at them.
    Returns True if it changed."""
    old_dir = Path(app_row.app_dir)
    if old_dir.exists() or APPS_BASE_DIR in old_dir.parents:
        return False
    here = APPS_BASE_DIR.joinpath(*old_dir.parts[-2:])   # apps/<user id>/<slug>
    if len(old_dir.parts) < 3 or old_dir.parts[-3] != APPS_BASE_DIR.name or not here.is_dir():
        return False
    try:
        venv_rel = Path(app_row.venv_path).relative_to(old_dir)
    except ValueError:
        venv_rel = Path("venv")
    logging.warning(f"App '{app_row.name}': data directory moved, {old_dir} -> {here}")
    app_row.app_dir = str(here)
    app_row.venv_path = str(here / venv_rel)
    return True


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    load_operation_state()

    await _check_python3_venv()
    await _check_rust()
    await asyncio.to_thread(_systemd_run_available)   # warm the (blocking) probe off the event loop

    # Apps that are still running from a Venvy process that did not get to
    # shut down (killed, out of memory, power loss with the apps in their own
    # systemd scopes). Their output pipe died with that process — the next
    # line they print is an EPIPE — and nothing is collecting their logs or
    # metrics. They are restarted below, so that what runs is supervised.
    orphaned = []
    with Session(engine) as session:
        _boot_reset_time = utc_now()
        for app_row in session.exec(select(App)).all():
            modified = _rebase_app_paths(app_row)
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
                if _app_pid_alive(app_row):
                    orphaned.append((app_row.id, app_row.name))
                else:
                    logging.info(f"Clearing stale status for '{app_row.name}'")
                    app_row.status = "stopped"
                    app_row.pid = None
                    app_row.pid_start = None
                    app_row.last_manually_stopped_at = _boot_reset_time
                    modified = True

            if modified:
                session.add(app_row)
        session.commit()

    for app_id, name in orphaned:
        logging.warning(f"'{name}' was left running by a previous Venvy process — restarting it under supervision.")
        try:
            await restart_app_process_logic(app_id)
        except Exception as exc:
            logging.error(f"Could not restart '{name}': {exc}")

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
    await asyncio.gather(supervisor_task_handle, metrics_task_handle, return_exceptions=True)

    logging.info("Venvy shutting down — stopping all managed apps…")
    await terminal_manager.close_for_app(None, timeout=3.0)

    # Every app goes through the same stop as the Stop button: its systemd
    # scope if it has one, then its process group, SIGTERM first and SIGKILL
    # after the grace period, with its last output drained into the log. All
    # at once, and bounded: the service manager allows 30 s for all of this.
    with Session(engine) as session:
        to_stop = {a.id for a in session.exec(select(App)).all()
                   if a.pid or a.status in ("running", "starting", "stopping")}
    to_stop |= set(_active_app_pids)

    async def _stop(app_id: int) -> None:
        try:
            with Session(engine) as s:
                await stop_app_process_logic(app_id, s)
        except Exception as exc:
            logging.warning(f"Could not stop app {app_id} cleanly: {exc}")

    if to_stop:
        stops = [asyncio.create_task(_stop(app_id)) for app_id in to_stop]
        _done, unfinished = await asyncio.wait(stops, timeout=_STOP_GRACE_SEC + 15)
        for t in unfinished:
            t.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)

    # Whatever is still there after that is killed outright.
    own_pgid = os.getpgid(0)
    for apid in list(_active_app_pids.values()):
        if _pid_alive(apid):
            try:
                pgid = os.getpgid(apid)
                # Never Venvy's own process group.
                if pgid != own_pgid:
                    os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass

    with Session(engine) as session:
        for app_row in session.exec(select(App).where(App.status.in_(("running", "starting", "stopping")))).all():
            app_row.status = "stopped"
            app_row.pid = None
            app_row.pid_start = None
            app_row.systemd_scope = None
            if app_row.last_stop_time is None:
                app_row.last_stop_time = utc_now()
            session.add(app_row)
        session.commit()

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


def _add_security_headers(response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # "0", not "1; mode=block": the legacy XSS auditor this header drives
    # was itself exploitable, and browsers that still honour the header
    # are best told to keep it off. The CSP below is the real protection.
    response.headers.setdefault("X-XSS-Protection", "0")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
    if SESSION_COOKIE_SECURE:
        # Set by the operator once Venvy is only reached over HTTPS: from
        # then on a browser should not try plain HTTP for this host at all.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add conservative security headers to every response."""
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        _add_security_headers(response)
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
            if not csrf_cookie or not csrf_header or not secrets.compare_digest(
                    csrf_cookie.encode("utf-8"), csrf_header.encode("utf-8")):
                # This answer never reaches SecurityHeadersMiddleware below.
                response = JSONResponse({"detail": "CSRF validation failed"}, status_code=403)
                _add_security_headers(response)
                return response
        response = await call_next(request)
        if "csrf_token" not in request.cookies:
            token = secrets.token_urlsafe(32)
            response.set_cookie("csrf_token", token, httponly=False, samesite="lax", path="/", secure=SESSION_COOKIE_SECURE)
        return response

app.add_middleware(CSRFMiddleware)


# Request bodies are read (and parsed, or spooled to disk) before a route's
# own checks run — including the one for being signed in. Without a ceiling
# anyone who can reach the port could post gigabytes at the login endpoint
# and have the server hold them in memory.
_BODY_LIMIT_DEFAULT = 16 * 1024 * 1024


_BODY_LIMIT_CHUNK = _UPLOAD_CHUNK_MAX + 1024 * 1024   # one upload chunk plus its form fields


# The single-request upload endpoints kept for API clients: several files of
# up to _SINGLE_REQUEST_UPLOAD_MAX each.
_BODY_LIMIT_SINGLE_UPLOAD = 8 * _SINGLE_REQUEST_UPLOAD_MAX


_SINGLE_UPLOAD_PATH_RE = re.compile(r"\A/api/apps/(?:upload|\d+/files/upload)/?\Z")


def _body_limit(path: str) -> int:
    if path.rstrip("/").endswith("/upload/chunk"):
        return _BODY_LIMIT_CHUNK
    if _SINGLE_UPLOAD_PATH_RE.match(path):
        return _BODY_LIMIT_SINGLE_UPLOAD
    return _BODY_LIMIT_DEFAULT


class BodySizeLimitMiddleware:
    """Refuse request bodies over the limit for their path — by their
    Content-Length when there is one, and by counting as they arrive when
    there isn't (chunked transfer encoding). Outermost, so nothing has been
    read by the time it says no."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        limit = _body_limit(scope["path"])
        too_large = HTTPException(413, f"Request body is larger than the {limit // (1024 * 1024)} MB allowed here.")
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                over = int(declared) > limit
            except ValueError:
                over = False   # malformed: the HTTP layer rejects it itself
            if over:
                body = json.dumps({"detail": too_large.detail}).encode("utf-8")
                await send({"type": "http.response.start", "status": 413, "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"connection", b"close"),
                ]})
                await send({"type": "http.response.body", "body": body})
                return

        received = 0

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Surfaces in the route's body parsing, which turns an
                    # HTTPException into the response.
                    raise too_large
            return message

        await self.app(scope, counting_receive, send)


app.add_middleware(BodySizeLimitMiddleware)

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
