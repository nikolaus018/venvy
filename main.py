"""
Venvy backend — dependency check.
If a required package is missing, we print a clear error instead of
silently installing at runtime (which can corrupt a running venv and
blocks the event loop on startup).

-------------------------------------------------------------------------------
Backup & restore procedure
-------------------------------------------------------------------------------
Venvy's persistent state lives under the directory Venvy is launched from
(DATA_DIR = the current working directory at startup): `data.db` (SQLite —
users, sessions, app metadata, settings), `apps/` (every hosted app's code,
venv, and logs), and `.venvy_key` (the local symmetric-encryption key used
to decrypt Cloudflare credentials and app env vars stored in `data.db` —
without it, those encrypted columns are unrecoverable; treat it like a
private key).

Normal case — Venvy is alive, you want a backup or to roll back: use the
UI (System → Database Backups), or hit the API directly:
  - `POST /api/system/backup` bundles a consistent `data.db` snapshot (via
    `VACUUM INTO` — safe on a live WAL-mode database, unlike a raw `cp`),
    `.venvy_key`, and all of `apps/` into one `full-*.tar.gz` in `backups/`.
    One file is a complete backup; no shell access to the server needed.
  - `POST /api/system/backup/restore/{filename}` rolls this same live
    server back to one of those archives in place: stops every running
    app, swaps in the backup's files, and restarts the process. A safety
    backup of the pre-restore state is taken automatically first.

Disaster case — the host itself is gone and you're standing up a
replacement, so there's no running Venvy to call the API on: this still
requires manual file placement, same as always —
  1. Stop Venvy on the new host (if it's already running against a fresh DB).
  2. Untar a `full-*.tar.gz` backup (or place a manually-copied `data.db` +
     `.venvy_key` + `apps/`) into the new working directory.
  3. Start Venvy — it picks up the restored files.
-------------------------------------------------------------------------------
"""
import os as _os; _os.environ.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")

import sys as _bootstrap_sys

_REQUIRED = ['cryptography', 'aiofiles', 'uvicorn', 'fastapi', 'sqlmodel', 'aiohttp', 'multipart']
if not getattr(_bootstrap_sys, 'frozen', False):
    _missing = [m for m in _REQUIRED if not __import__('importlib.util', fromlist=['']).find_spec(m)]
    if _missing:
        print(
            f"[Venvy] Missing packages: {', '.join(_missing)}\n"
            f"  Run: {_bootstrap_sys.executable} -m pip install {' '.join(_missing)}",
            file=_bootstrap_sys.stderr,
        )
        _bootstrap_sys.exit(1)

import asyncio
import unicodedata
import base64
import contextlib
import datetime
import fcntl
import hashlib
import hmac
import json
import logging
import os
import pty
import re
import secrets
import shutil
import signal
import socket
import struct
import sys
import tarfile
import termios
import threading
import time
import urllib.parse
import argparse
import subprocess
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Set, Union
import aiohttp


import aiofiles
import uvicorn
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from starlette.requests import HTTPConnection
from pydantic import BaseModel
from sqlmodel import Field, Session, SQLModel, create_engine, select

# venvy imports
from venvy.api import terminal as api_terminal
from venvy.api import apps as api_apps
from venvy.api import system as api_system
from venvy.api import cloudflare as api_cloudflare
from venvy.api import github as api_github
from venvy.api import files as api_files
from venvy.api import users as api_users
from venvy.api import auth as api_auth
from venvy.apps import (
    _clean_app_name,
    sanitize_slug,
    _app_safe,
)
from venvy.uploads import (
    _SINGLE_REQUEST_UPLOAD_MAX,
    _UPLOAD_CHUNK_MAX,
    _UPLOAD_FILE_MAX,
    _UPLOAD_STALE_SEC,
    _UPLOAD_ID_RE,
    _UPLOAD_COPY_BUF,
    _save_upload_file,
    _purge_stale_upload_parts,
    _upload_part_path,
)
from venvy.github import (
    _GH_DEVICE_CODE_URL,
    _GH_TOKEN_URL,
    _GH_API_BASE,
    _gh_device_flows,
    _gh_client_id,
    _gh_token,
    _gh_setting,
    _gh_save,
    _git_auth_config,
    _run_git,
    _git_status_payload,
)
from venvy.settings import (
    _is_secret_setting,
    _reject_reserved_setting,
    _setting_for_storage,
)
from venvy.cloudflare import (
    _sanitize_cf_value,
    _get_cf_creds,
    _cf_request,
    _CF_HOSTNAME_RE,
    _clean_cf_hostname,
    _cf_find_zone_id,
    _cf_upsert_dns_cname,
    _cf_delete_dns_cname,
)
from venvy.backup import (
    _create_full_backup_archive,
    _BACKUP_KEEP,
    _BACKUP_TYPES,
    _BACKUP_FILENAME_RE,
    _FULL_BACKUP_FILENAME_RE,
    _resolve_backup_path,
    _restore_tar_filter,
    _self_restart,
    _restore_job,
    _run_restore,
)
from venvy.terminal import (
    _write_bashrc,
    _TERMINAL_RECHECK_SEC,
    TerminalManager,
    terminal_manager,
)
from venvy.process import (
    _active_app_pids,
    _app_metrics,
    host_cpu_percent,
    _psutil_net_connections,
    _detect_ports_sync,
    find_free_port,
    _verify_port_free,
    get_server_ipv4,
    _build_app_env,
    _log_tasks,
    _trigger_supervisor,
    _cancel_log_task,
    _finish_log_task,
    _LOG_MAX_BYTES,
    _LOG_KEEP_ROTATED,
    _LOG_MAX_LINE,
    _rotate_log,
    log_streamer,
    _pid_alive,
    _pid_alive_async,
    _proc_start_id,
    _app_pid_alive,
    _systemd_run_available,
    _app_locks,
    _app_lock,
    start_app_process_logic,
    _start_app_process,
    _kill_pid_tree,
    stop_app_process_logic,
    _stop_app_process,
    _restart_backoff,
    _RESTART_STABLE_SEC,
    _RESTART_MAX_DELAY,
    _restart_allowed,
    restore_in_progress,
    set_restore_in_progress,
    supervisor_task,
    _collect_metrics_tick,
    metrics_collector_task,
)
from venvy.jobs import (
    _running_tasks,
    _jobs,
    _JOB_TTL_SEC,
    _start_job,
)
from venvy.isolation import (
    _APP_USERS_REQUESTED,
    _APP_USERS_ACTIVE,
    _isolation_active,
    _app_username,
    _app_ids,
    _chown_tree,
    _ensure_app_user,
    _delete_app_user,
    _chown_app_dir,
    _app_proc_kwargs,
    _app_tool_env,
    _app_log_file,
)
from venvy.safefs import (
    is_safe_path,
    _PROTECTED_APP_FILE_PREFIXES,
    _is_protected_app_file,
    _upload_rel_path_error,
    _O_NOFOLLOW_DIR,
    _O_CLOEXEC,
    _FS_COPY_BUF,
    AppPathError,
    _app_rel_parts,
    _open_app_dir,
    _fs_lstat,
    _fs_is_dir,
    _fs_write_new,
    _fs_read_chunks,
    _fs_save,
    _fs_write_stream,
    _fs_install,
    _fs_touch,
    _fs_mkdir,
    _fs_rmtree,
    _fs_delete,
    _fs_copy_entry,
    _fs_move_or_copy,
    _fs_open_file,
    _fs_listdir,
    _file_download_response,
)
from venvy.auth import (
    _totp_generate_secret,
    _totp_hotp,
    _totp_verify,
    _totp_uri,
    _resolve_client_ip,
    check_ip_allowlist,
    hash_password,
    verify_password,
    _DUMMY_PASSWORD_HASH,
    _validate_password_strength,
    _clean_username,
    _hash_token,
    _new_session,
    _load_session,
    _authenticate,
    get_current_user,
    _download_tickets,
    _download_tickets_lock,
    _DOWNLOAD_TICKET_TTL,
    get_download_user,
    check_admin,
    require_operator,
    _login_attempts,
    _login_lock,
    _LOGIN_MAX_TRIES,
    _LOGIN_LOCKOUT_SEC,
    _LOGIN_MAX_TRIES_PER_USER,
    _LOGIN_MAX_TRACKED,
    _login_rate_key,
    _check_login_rate,
    _login_user_key,
    _record_login_failure,
    _clear_login_rate,
    _purge_login_attempts,
    _mutation_hits,
    _mutation_lock,
    _MUTATION_MAX_PER_WINDOW,
    _MUTATION_WINDOW_SEC,
    rate_limit_mutations,
)
from venvy.db import (
    engine,
    set_sqlite_pragma,
    create_db_and_tables,
    _lock_down_data_files,
    get_session,
)
from venvy.models import (
    VALID_ROLES,
    User,
    UserSession,
    App,
    SystemSetting,
    CfRoute,
)
from venvy.audit import (
    audit_log,
)
from venvy.crypto import (
    _ENC_PREFIX,
    _fernet_instance,
    _fernet_lock,
    _get_fernet,
    _encrypt_secret,
    _try_decrypt_secret,
    _decrypt_secret,
)
from venvy.config import (
    ASSETS_DIR,
    _ALLOWED_ORIGINS,
    psutil,
    DATA_DIR,
    APPS_BASE_DIR,
    BACKUP_DIR,
    DB_FILE,
    DATABASE_URL,
    SESSION_TTL_SECONDS,
    SESSION_COOKIE_SECURE,
    _VENVY_KEY_FILE,
    IP_ALLOWLIST_RAW,
    IP_ALLOWLIST,
    TRUSTED_PROXY_IPS,
    PROXY_SECRET,
    _PRIVATE_ENV_VARS,
    utc_now,
    _VENVY_BOOT_TIME,
    _make_tz_aware,
    TERMINAL_RC_DIR,
    APP_LOGS_DIR,
    _UPLOAD_TMP_DIR,
)


# ---------------------------------------------------------------------------
# Startup dependency checks
# ---------------------------------------------------------------------------

async def _ensure_python3_venv() -> None:
    """Verify that `python -m venv` works, otherwise auto-install."""
    test_dir = DATA_DIR / ".venv_probe"
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", "venv", "--without-pip", str(test_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            logging.info("python3-venv: OK")
            return
        err_text = stderr.decode(errors="replace").strip()
        logging.warning(f"python3-venv probe failed (rc={proc.returncode}): {err_text}")
    except Exception as exc:
        logging.warning(f"python3-venv probe error: {exc}")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)

    minor = sys.version_info.minor
    packages_to_try = [f"python3.{minor}-venv", "python3-venv"]

    logging.warning(
        f"python3-venv is not available — attempting: "
        f"apt-get install -y {' '.join(packages_to_try)}"
    )

    for pkg in packages_to_try:
        try:
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            proc = await asyncio.create_subprocess_exec(
                "apt-get", "install", "-y", "--no-install-recommends", pkg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                logging.info(f"Successfully installed {pkg} via apt-get.")
                return
            
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", pkg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                logging.info(f"Successfully installed {pkg} via sudo apt-get.")
                return
        except Exception as exc:
            logging.warning(f"apt-get install {pkg} error: {exc}")

    logging.error(
        "Could not auto-install python3-venv. "
        f"Please run: sudo apt-get install -y python3.{minor}-venv\n"
        "App creation will fail until this is resolved."
    )


async def _ensure_rust() -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "cargo", "-V",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            logging.info("cargo: OK")
            return
    except Exception:
        pass

    logging.warning("cargo is missing — attempting apt-get install -y cargo rustc")
    try:
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        proc = await asyncio.create_subprocess_exec(
            "apt-get", "install", "-y", "--no-install-recommends", "cargo", "rustc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            logging.info("Successfully installed cargo and rustc via apt-get.")
            return
        
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "cargo", "rustc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            logging.info("Successfully installed cargo and rustc via sudo apt-get.")
            return
    except Exception as exc:
        logging.warning(f"Failed to install rust: {exc}")


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()

    await _ensure_python3_venv()
    await _ensure_rust()

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


from starlette.middleware.base import BaseHTTPMiddleware

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
            "img-src 'self' data: blob: https://avatars.githubusercontent.com; "
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

api_router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist)]
)


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------

app.include_router(api_auth.router)
app.include_router(api_users.router)
app.include_router(api_files.router)
app.include_router(api_github.router)
app.include_router(api_cloudflare.router)
app.include_router(api_system.router)
app.include_router(api_apps.router)
app.include_router(api_terminal.router)
app.include_router(api_router)


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


@app.get("/sw.js")
async def serve_service_worker():
    """The PWA service worker. Has to come from the site root to be allowed
    to control it, and must not be cached hard or updates never arrive."""
    path = ASSETS_DIR / "sw.js"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/")
async def serve_spa():
    html = ASSETS_DIR / "index.html"
    if html.exists():
        return FileResponse(html)
    return PlainTextResponse("Frontend not found.", status_code=404)

def _print_banner(host: str, port: int):
    """Print a stylized Venvy ASCII banner on startup."""
    indigo = "\033[38;5;63m"
    gray = "\033[90m"
    bold = "\033[1m"
    reset = "\033[0m"
    
    banner = f"""
{indigo}{bold}    ██╗   ██╗███████╗███╗   ██╗██╗   ██╗██╗   ██╗
    ██║   ██║██╔════╝████╗  ██║██║   ██║╚██╗ ██╔╝
    ██║   ██║█████╗  ██╔██╗ ██║██║   ██║ ╚████╔╝ 
    ╚██╗ ██╔╝██╔══╝  ██║╚██╗██║╚██╗ ██╔╝  ╚██╔╝  
     ╚████╔╝ ███████╗██║ ╚████║ ╚████╔╝    ██║   
      ╚═══╝  ╚══════╝╚═╝  ╚═══╝  ╚═══╝     ╚═╝   {reset}
    {gray}Premium Application Manager — v4.0.0{reset}
    
    {indigo}➜{reset}  {bold}Local:{reset}    http://127.0.0.1:{port}
    {indigo}➜{reset}  {bold}Network:{reset}  http://{host}:{port}
    {gray}──────────────────────────────────────────────────{reset}
    """
    print(banner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Venvy Backend Server")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "54783")), help="Port to bind to")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"), help="Logging level")
    
    args = parser.parse_args()

    _print_banner(args.host if args.host != "0.0.0.0" else get_server_ipv4(), args.port)
    import importlib.util as _ilu
    if not (_ilu.find_spec("websockets") or _ilu.find_spec("wsproto")):
        logging.warning(
            "No WebSocket library installed — the in-browser terminal won't connect. "
            "Install it with: %s -m pip install 'uvicorn[standard]'", sys.executable)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not SESSION_COOKIE_SECURE:
        logging.warning(
            "Venvy is listening on %s over plain HTTP: passwords and session cookies "
            "cross the network unencrypted. Prefer binding to 127.0.0.1 and reaching it "
            "through the Cloudflare tunnel (HTTPS), then set SESSION_COOKIE_SECURE=1.", args.host)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        # Never let X-Forwarded-For rewrite the client address — see
        # _resolve_client_ip() for how tunnel traffic is attributed instead.
        proxy_headers=False,
        access_log=True,
    )