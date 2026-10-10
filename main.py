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

try:
    import tomllib as _toml
except ImportError:
    # Fallback for Python < 3.11 (though Venvy recommends 3.11+)
    _toml = None

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
# Request / response models
# ---------------------------------------------------------------------------

class AppUpdateReq(BaseModel):
    name: Optional[str] = None
    start_on_boot: Optional[bool] = None
    restart_policy: Optional[str] = None
    runtime: Optional[str] = None
    port: Optional[int] = None
    manual_ports: Optional[List[int]] = None
    env_vars: Optional[Dict[str, str]] = None
    entrypoint: Optional[str] = None
    url_source_pref: Optional[str] = None
    memory_limit_mb: Optional[int] = None
    cpu_limit_percent: Optional[int] = None


class SystemSettingReq(BaseModel):
    value: str


class PackageReq(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# App endpoints
# ---------------------------------------------------------------------------

@api_router.get("/apps")
def list_apps(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    # Apps belong to the instance, not to the logged-in user — every authenticated
    # user sees every app (viewers are read-only; enforcement happens on the
    # mutating routes below via require_operator).
    apps = session.exec(select(App).order_by(App.name)).all()
    changed = False
    for app_row in apps:
        if app_row.status in ("starting", "stopping"):
            continue
        if app_row.status == "running" and app_row.pid:
            if not _app_pid_alive(app_row):
                app_row.status = "stopped"
                app_row.pid = None
                app_row.pid_start = None
                app_row.last_stop_time = utc_now()
                session.add(app_row)
                changed = True
        elif app_row.status == "stopped" and app_row.pid:
            app_row.pid = None
            session.add(app_row)
            changed = True
    if changed:
        session.commit()
    can_see_secrets = user.role in ("admin", "operator")
    return [_app_safe(a, reveal_secrets=can_see_secrets) for a in apps]


@api_router.get("/system/metrics")
def get_system_metrics(user: User = Depends(check_admin)):
    """Return host-level resource statistics."""
    if psutil is None:
        return {"error": "psutil not installed"}
    
    cpu_percent = host_cpu_percent()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    load = [0.0, 0.0, 0.0]
    try:
        load = os.getloadavg()
    except Exception:
        pass
    
    return {
        "cpu": {
            "percent": cpu_percent, 
            "load": load, 
            "cores": psutil.cpu_count(logical=True)
        },
        "memory": {"total": mem.total, "available": mem.available, "used": mem.used, "percent": mem.percent},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": disk.percent},
        "local_ip": get_server_ipv4(),
        "uptime": int(time.time() - psutil.boot_time()) if hasattr(psutil, "boot_time") else 0
    }


@api_router.post("/system/backup")
async def create_backup(
    admin: User = Depends(check_admin),
    _rl: None = Depends(rate_limit_mutations),
):
    """Create a full disaster-recovery backup — see _create_full_backup_archive().
    Answers {"job_id"}; tarring every app can take well over a minute."""
    async def _job() -> dict:
        dest = await asyncio.to_thread(_create_full_backup_archive)
        audit_log(admin.username, "system_full_backup", f"path={dest}")
        return {"ok": True, "path": str(dest), "size_bytes": dest.stat().st_size}
    return _start_job(admin, "backup", _job)


@api_router.get("/system/backups")
def list_backups(admin: User = Depends(check_admin)):
    """List existing backups of both kinds — see _BACKUP_TYPES."""
    if not BACKUP_DIR.exists():
        return []
    entries = [
        {
            "filename": f.name,
            "size_bytes": f.stat().st_size,
            "created_at": datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc).isoformat(),
            "type": kind,
        }
        for pattern, kind in _BACKUP_TYPES
        for f in BACKUP_DIR.glob(pattern)
    ]
    return sorted(entries, key=lambda b: b["created_at"], reverse=True)


@api_router.get("/system/backups/{filename}/download")
def download_backup(filename: str, admin: User = Depends(get_download_user)):
    """Download a previously created backup."""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    path = _resolve_backup_path(filename)
    audit_log(admin.username, "system_backup_download", f"path={path}")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
        },
    )


@api_router.delete("/system/backups/{filename}")
def delete_backup(
    filename: str,
    admin: User = Depends(check_admin),
    _rl: None = Depends(rate_limit_mutations),
):
    """Delete a backup."""
    path = _resolve_backup_path(filename)
    path.unlink()
    audit_log(admin.username, "system_backup_delete", f"path={path}")
    return {"ok": True}


class RestoreBackupReq(BaseModel):
    confirm: str


@api_router.post("/system/backup/restore/{filename}")
async def restore_backup(
    filename: str,
    req: RestoreBackupReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
    _rl: None = Depends(rate_limit_mutations),
):
    """Roll this live server back to a full backup: stops every running app,
    replaces data.db / apps/ / the encryption key with the backup's
    contents, then restarts the Venvy process so it comes back up clean.

    Destructive and only partly undoable — a fresh full backup of the
    CURRENT state is taken automatically before anything else is touched,
    so the restore itself can be reversed, but any apps running at the time
    are stopped and stay stopped. Requires confirm == "RESTORE" or this is
    a no-op 400; only full backups (not legacy DB-only ones) are restorable.
    """
    if req.confirm != "RESTORE":
        raise HTTPException(400, 'Confirmation phrase did not match — send {"confirm": "RESTORE"}.')
    if restore_in_progress():
        raise HTTPException(409, "A restore is already in progress.")
    if not _FULL_BACKUP_FILENAME_RE.match(filename):
        raise HTTPException(400, "Only full backups can be restored — legacy database-only backups are download-only.")
    archive_path = BACKUP_DIR / filename
    if not is_safe_path(BACKUP_DIR, archive_path) or not archive_path.is_file():
        raise HTTPException(404, "Backup not found.")

    audit_log(admin.username, "system_restore_started", f"path={archive_path}")
    session.close()
    set_restore_in_progress(True)
    return _start_job(admin, "restore", lambda: _restore_job(admin, archive_path))


@api_router.post("/apps/upload")
async def upload_app(
    name: str = Form(...),
    runtime: str = Form("python"),
    entrypoint: str = Form(""),
    start_on_boot: bool = Form(False),
    restart_policy: str = Form("never"),
    files: Optional[List[UploadFile]] = File(None),
    requirements: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    if runtime not in ("python", "rust"):
        raise HTTPException(400, "runtime must be one of: python, rust")
    if restart_policy not in ("never", "on-failure", "always"):
        raise HTTPException(400, "restart_policy must be one of: never, on-failure, always")
    name = _clean_app_name(name)

    entrypoint_clean = ""
    if runtime == "rust":
        entrypoint_clean = "main.rs"
    elif entrypoint.strip():
        entrypoint_strip = entrypoint.strip()
        entrypoint_clean = Path(entrypoint_strip).name
        if not entrypoint_clean or entrypoint_clean != entrypoint_strip:
            raise HTTPException(400, "Entrypoint must be a plain filename (e.g. main.py), not a path.")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", entrypoint_clean):
            raise HTTPException(400, "Entrypoint filename contains invalid characters.")

    slug = sanitize_slug(name)
    user_apps_dir = APPS_BASE_DIR / str(user.id)
    await asyncio.to_thread(user_apps_dir.mkdir, exist_ok=True)

    app_dir = user_apps_dir / slug
    if await asyncio.to_thread(app_dir.exists):
        slug = f"{slug}-{int(time.time())}"
        app_dir = user_apps_dir / slug
    await asyncio.to_thread(app_dir.mkdir, parents=True, mode=0o750)
    if not _isolation_active():
        await asyncio.to_thread((app_dir / "logs").mkdir, mode=0o750)

    skipped_files: List[Dict[str, str]] = []
    for file in (files or []):
        rel_path = file.filename or "upload"
        reason = _upload_rel_path_error(rel_path)
        dest = (app_dir / rel_path).resolve()
        if not reason and not is_safe_path(app_dir, dest):
            reason = "path escapes app directory"
        if reason:
            skipped_files.append({"path": rel_path, "reason": reason})
            continue
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        if not await asyncio.to_thread(_save_upload_file, file, dest, _SINGLE_REQUEST_UPLOAD_MAX):
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(413, f"File '{rel_path}' exceeds the {_SINGLE_REQUEST_UPLOAD_MAX // (1024 * 1024)} MB single-request limit.")
    req_bytes = await requirements.read() if requirements and requirements.filename else None

    venv_path = app_dir / "venv"
    new_app = App(
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
        status="installing",
    )
    session.add(new_app)
    session.commit()
    session.refresh(new_app)
    app_id = new_app.id
    audit_log(user.username, "app_create", f"created app '{new_app.name}' (id={app_id})")

    async def _finish() -> dict:
        """venv + pip install, which can take minutes — runs as a job."""
        try:
            with Session(engine) as s2:
                row = s2.get(App, app_id)
            await asyncio.to_thread(_ensure_app_user, row)
            kw = _app_proc_kwargs(row)
            env = _app_tool_env(row)
            if runtime == "python":
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-m", "venv", str(venv_path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **kw,
                )
                try:
                    _, venv_err = await asyncio.wait_for(proc.communicate(), timeout=120)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise HTTPException(500, "venv creation timed out.")
                if proc.returncode != 0:
                    err_msg = venv_err.decode(errors="replace")
                    hint = ""
                    if "ensurepip" in err_msg or "venv" in err_msg.lower():
                        hint = f" Hint: python3-venv may not be installed — run: sudo apt-get install -y python3.{sys.version_info.minor}-venv"
                    raise HTTPException(500, f"venv creation failed: {err_msg}{hint}")

            if req_bytes is not None and runtime == "python":
                req_path = app_dir / "requirements.txt"
                await asyncio.to_thread(_fs_save, app_dir, ("requirements.txt",), req_bytes, _app_ids(row))
                pip_bin = venv_path / "bin" / "pip"
                if not await asyncio.to_thread(pip_bin.exists):
                    raise HTTPException(500, "pip not found in the new virtual environment.")
                proc = await asyncio.create_subprocess_exec(
                    str(pip_bin), "install", "--no-input", "-r", str(req_path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **kw,
                )
                try:
                    _, pip_err = await asyncio.wait_for(proc.communicate(), timeout=600)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise HTTPException(500, "pip install timed out after 10 minutes.")
                if proc.returncode != 0:
                    raise HTTPException(500, f"pip install failed: {pip_err.decode(errors='replace')}")

            with Session(engine) as s2:
                row = s2.get(App, app_id)
                row.status = "stopped"
                s2.add(row)
                s2.commit()
                s2.refresh(row)
                result = _app_safe(row)
            result["skipped_files"] = skipped_files
            return result
        except BaseException:
            # Failed or cancelled: remove the half-created app entirely.
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            with Session(engine) as s2:
                row = s2.get(App, app_id)
                if row:
                    s2.delete(row)
                    s2.commit()
            await asyncio.to_thread(_delete_app_user, app_id)
            raise

    return {**_start_job(user, "app_create", _finish), "app_id": app_id, "skipped_files": skipped_files}


@api_router.post("/apps/{app_id}/control")
async def control_app(
    app_id: int,
    action: str = Query(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "Invalid action. Use: start | stop | restart")
    if app_row.status == "installing":
        raise HTTPException(409, "This app is still being set up — wait for its install to finish.")
    if restore_in_progress() and action != "stop":
        raise HTTPException(409, "A restore is in progress — apps can be started again once Venvy has restarted.")
    audit_log(user.username, f"app_{action}", f"app '{app_row.name}' (id={app_id})")

    async def _do_start():
        try:
            with Session(engine) as s:
                await start_app_process_logic(app_id, s)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background start failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a:
                    a.status = "error"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_stop():
        try:
            with Session(engine) as s:
                await stop_app_process_logic(app_id, s, manual=True)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background stop failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a and a.status == "stopping":
                    a.status = "running" if a.pid else "stopped"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_restart():
        try:
            # One lock hold for both halves, so nothing else can start the
            # app in the gap between them.
            async with _app_lock(app_id):
                with Session(engine) as s:
                    await _stop_app_process(app_id, s, manual=False)
                with Session(engine) as s:
                    await _start_app_process(app_id, s)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background restart failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a:
                    a.status = "error"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    _restart_backoff.pop(app_id, None)
    if action == "start":
        if app_row.status != "running":
            app_row.status = "starting"
            session.add(app_row)
            session.commit()
        t = asyncio.create_task(_do_start())
        _running_tasks.add(t)
    elif action == "stop":
        if app_row.status != "stopped":
            app_row.status = "stopping"
            session.add(app_row)
            session.commit()
        t = asyncio.create_task(_do_stop())
        _running_tasks.add(t)
    elif action == "restart":
        app_row.status = "stopping"
        session.add(app_row)
        session.commit()
        t = asyncio.create_task(_do_restart())
        _running_tasks.add(t)

    return {"status": app_row.status, "queued": True}


@api_router.put("/apps/{app_id}")
async def update_app(
    app_id: int,
    req: AppUpdateReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if req.name is not None:
        app_row.name = _clean_app_name(req.name)
    if req.start_on_boot is not None:
        app_row.start_on_boot = req.start_on_boot
    if req.restart_policy is not None:
        if req.restart_policy not in ("never", "on-failure", "always"):
            raise HTTPException(400, "restart_policy must be one of: never, on-failure, always")
        app_row.restart_policy = req.restart_policy
    if req.port is not None:
        if req.port != 0 and not (1 <= req.port <= 65535):
            raise HTTPException(400, "Port must be between 1 and 65535 (or 0 to clear).")
        app_row.port = None if req.port == 0 else req.port
    if req.manual_ports is not None:
        valid_ports = []
        for p in req.manual_ports:
            if not (1 <= p <= 65535):
                raise HTTPException(400, f"Invalid port: {p}")
            valid_ports.append(p)
        app_row.manual_ports = json.dumps(valid_ports)
        if valid_ports:
            app_row.port = valid_ports[0]
        elif req.manual_ports == []:
            app_row.port = None

    manual_list = []
    try:
        manual_list = json.loads(app_row.manual_ports or "[]")
    except Exception:
        pass
    
    auto_ports = []
    try:
        auto_ports = json.loads(app_row.detected_ports or "[]")
    except Exception:
        pass

    if manual_list:
        combined = manual_list
    else:
        combined = auto_ports
    
    app_row.detected_ports = json.dumps(combined)
    if not app_row.port and combined:
        app_row.port = combined[0]

    if req.env_vars is not None:
        for env_key, env_value in req.env_vars.items():
            # Anything else can't be put in a process environment at all, and
            # only failed later, as an unexplained error on start.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
                raise HTTPException(400, f"Invalid environment variable name: {env_key!r}")
            if "\x00" in env_value:
                raise HTTPException(400, f"Environment variable {env_key} contains a NUL character.")
        app_row.env_json = _encrypt_secret(json.dumps(req.env_vars))
        audit_log(user.username, "app_env_change", f"app '{app_row.name}' (id={app_id})")
    # Runtime first: the entrypoint rules below depend on it, and a request
    # that switches a Rust app to Python *and* names its entrypoint used to
    # have that entrypoint overwritten with main.rs.
    if req.runtime is not None:
        if req.runtime not in ("python", "rust"):
            raise HTTPException(400, "runtime must be one of: python, rust")
        app_row.runtime = req.runtime
        if app_row.runtime == "rust":
            app_row.entrypoint = "main.rs"
    if req.entrypoint is not None:
        if app_row.runtime == "rust":
            app_row.entrypoint = "main.rs"
        else:
            ep_strip = req.entrypoint.strip()
            if ep_strip == "":
                app_row.entrypoint = ""
            else:
                ep_clean = Path(ep_strip).name
                if not ep_clean or ep_clean != ep_strip:
                    raise HTTPException(400, "Entrypoint must be a plain filename (e.g. main.py).")
                if not re.fullmatch(r"[A-Za-z0-9_.\-]+", ep_clean):
                    raise HTTPException(400, "Entrypoint filename contains invalid characters.")
                app_row.entrypoint = ep_clean
    if req.url_source_pref is not None:
        if req.url_source_pref not in ("local", "public"):
            raise HTTPException(400, "url_source_pref must be 'local' or 'public'")
        app_row.url_source_pref = req.url_source_pref
    if req.memory_limit_mb is not None:
        if req.memory_limit_mb != 0 and not (16 <= req.memory_limit_mb <= 1_048_576):
            raise HTTPException(400, "memory_limit_mb must be between 16 and 1048576 (or 0 for unlimited).")
        app_row.memory_limit_mb = None if req.memory_limit_mb == 0 else req.memory_limit_mb
    if req.cpu_limit_percent is not None:
        if req.cpu_limit_percent != 0 and not (1 <= req.cpu_limit_percent <= 10000):
            raise HTTPException(400, "cpu_limit_percent must be between 1 and 10000 (or 0 for unlimited).")
        app_row.cpu_limit_percent = None if req.cpu_limit_percent == 0 else req.cpu_limit_percent

    app_row.updated_at = utc_now()
    session.add(app_row)
    session.commit()
    refreshed = session.get(App, app_id)
    return _app_safe(refreshed)


@api_router.delete("/apps/{app_id}")
async def delete_app(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if app_row.status == "installing":
        raise HTTPException(409, "This app is still being set up — wait for its install to finish.")
    app_name = app_row.name
    _cancel_log_task(app_id)
    await stop_app_process_logic(app_id, session)

    # An app has one route per published port. Every one of them has to go:
    # a hostname left in the tunnel keeps forwarding to that port, where the
    # next app to bind it would be exposed to the internet unasked.
    cf_routes = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all()
    published = {r.hostname for r in cf_routes if r.cf_route_id and r.hostname}
    if published:
        try:
            creds      = _get_cf_creds(session)
            account_id = creds["cf_account_id"]
            tunnel_id  = creds["cf_tunnel_id"]
            api_token  = creds["cf_api_token"]
            cfg_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                f"/cfd_tunnel/{tunnel_id}/configurations"
            )
            current_cfg = await _cf_request("GET", cfg_url, api_token)
            result_obj  = current_cfg.get("result") or {}
            ingress     = result_obj.get("config", {}).get("ingress") or []
            named       = [r for r in ingress if r.get("hostname") and r["hostname"] not in published]
            catch_all   = [r for r in ingress if not r.get("hostname")]
            new_ingress = named + (catch_all or [{"service": "http_status:404"}])
            await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}})
            for hostname in sorted(published):
                try:
                    zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
                    if zone_id:
                        await _cf_delete_dns_cname(zone_id, hostname, api_token)
                    logging.info(f"CF route cleaned up for deleted app {app_id} ({hostname})")
                except Exception as exc:
                    logging.warning(f"CF DNS cleanup failed for app {app_id} ({hostname}): {exc}")
        except Exception as exc:
            logging.warning(f"CF cleanup failed for app {app_id} during delete: {exc}")
    for cf_route in cf_routes:
        session.delete(cf_route)

    app_dir_path = Path(app_row.app_dir)
    if await asyncio.to_thread(app_dir_path.exists):
        await asyncio.to_thread(shutil.rmtree, app_row.app_dir, ignore_errors=True)
    await asyncio.to_thread(shutil.rmtree, APP_LOGS_DIR / str(app_id), True)
    (TERMINAL_RC_DIR / f"app-{app_id}.bashrc").unlink(missing_ok=True)
    await asyncio.to_thread(_delete_app_user, app_id)
    _restart_backoff.pop(app_id, None)
    session.delete(app_row)
    session.commit()
    _app_locks.pop(app_id, None)
    audit_log(user.username, "app_delete", f"deleted app '{app_name}' (id={app_id})")
    return {"ok": True}


@api_router.get("/apps/{app_id}/stats")
def app_stats(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    stats: Dict = {"cpu": 0.0, "memory": 0.0, "uptime": 0.0}
    if _app_pid_alive(app_row):
        # From the metrics collector: cpu_percent() on a freshly created
        # psutil.Process always reads 0.0, which is all this used to return,
        # and the collector's figures cover the app's child processes too.
        metrics = _app_metrics.get(app_id)
        if metrics:
            stats["cpu"] = metrics["cpu"]
            stats["memory"] = metrics["memory"] / (1024 * 1024)
        if app_row.last_start_time:
            stats["uptime"] = (utc_now() - _make_tz_aware(app_row.last_start_time)).total_seconds()
    return stats


@api_router.get("/apps/{app_id}/packages")
async def list_packages(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    
    if app_row.runtime == "rust":
        try:
            manifest, _ = _fs_open_file(Path(app_row.app_dir), ("Cargo.toml",))
        except AppPathError:
            return []
        if not _toml:
            manifest.close()
            raise HTTPException(500, "tomllib not available (requires Python 3.11+)")
        try:
            with manifest as f:
                data = _toml.load(f)
            deps = data.get("dependencies", {})
            dev_deps = data.get("dev-dependencies", {})
            
            results = []
            for name, val in {**deps, **dev_deps}.items():
                version = val if isinstance(val, str) else val.get("version", "unknown")
                results.append({"name": name, "version": version})
            return results
        except Exception as e:
            logging.error(f"Failed to parse Cargo.toml: {e}")
            return []

    if app_row.runtime != "python":
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")
        
    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, "Virtual environment python not found.")
    
    # The venv's python runs app-controlled code (.pth files, sitecustomize),
    # so it runs as the app's user, never as Venvy's.
    proc = await asyncio.create_subprocess_exec(
        str(venv_python), "-m", "pip", "list", "--format=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_app_tool_env(app_row),
        **_app_proc_kwargs(app_row),
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    try:
        return json.loads(stdout)
    except Exception:
        return []


async def _run_app_tool(app_row: App, argv: List[str], timeout: int, what: str) -> None:
    """Run a package-manager command for an app (as the app's user); raise
    HTTPException with its stderr on failure or timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_app_tool_env(app_row),
        **_app_proc_kwargs(app_row),
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, f"{what} timed out after {timeout} seconds.")
    if proc.returncode != 0:
        raise HTTPException(500, f"{what} failed: {stderr.decode(errors='replace')}")


@api_router.post("/apps/{app_id}/packages")
async def install_package(
    app_id: int,
    req: PackageReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Install a package. Answers {"job_id"} — poll GET /api/jobs/{id}."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    pkg_name = req.name.strip()

    if app_row.runtime == "rust":
        if not re.fullmatch(r"[A-Za-z0-9_.+\-]+(@[A-Za-z0-9_.+\-]+)?", pkg_name) or pkg_name.startswith("-"):
            raise HTTPException(400, "Invalid package name.")
        argv = ["cargo", "add", pkg_name, "--manifest-path", str(Path(app_row.app_dir) / "Cargo.toml")]
        timeout, what = 300, "cargo add"
    elif app_row.runtime == "python":
        venv_python = Path(app_row.venv_path) / "bin" / "python"
        if not venv_python.exists():
            raise HTTPException(500, "Virtual environment python not found.")
        # No leading "-": the name must never be read as a pip option.
        if pkg_name.startswith("-") or not re.fullmatch(
                r"[A-Za-z0-9_.+\-]+(\[[\w,]+\])?([\s]*(==|>=|<=|~=|!=|>|<)[^\s]*)?", pkg_name):
            raise HTTPException(400, "Invalid package name.")
        argv = [str(venv_python), "-m", "pip", "install", "--no-input", pkg_name]
        timeout, what = 600, "pip install"
    else:
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    async def _job() -> dict:
        await _run_app_tool(app_row, argv, timeout, what)
        audit_log(user.username, "package_install", f"app '{app_row.name}' (id={app_id}): {pkg_name}")
        return {"ok": True}

    return _start_job(user, "package_install", _job)


@api_router.delete("/apps/{app_id}/packages/{name}")
async def uninstall_package(
    app_id: int,
    name: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Uninstall a package. Answers {"job_id"} — poll GET /api/jobs/{id}."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name) or name.startswith("-"):
        raise HTTPException(400, "Invalid package name.")

    if app_row.runtime == "rust":
        argv = ["cargo", "remove", name, "--manifest-path", str(Path(app_row.app_dir) / "Cargo.toml")]
        timeout, what = 120, "cargo remove"
    elif app_row.runtime == "python":
        venv_python = Path(app_row.venv_path) / "bin" / "python"
        if not venv_python.exists():
            raise HTTPException(500, "Virtual environment python not found.")
        argv = [str(venv_python), "-m", "pip", "uninstall", "-y", name]
        timeout, what = 300, "pip uninstall"
    else:
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    async def _job() -> dict:
        await _run_app_tool(app_row, argv, timeout, what)
        audit_log(user.username, "package_uninstall", f"app '{app_row.name}' (id={app_id}): {name}")
        return {"ok": True}

    return _start_job(user, "package_uninstall", _job)


@api_router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user)):
    """Status of a background job: {status: running|done|error, result, error}."""
    job = _jobs.get(job_id)
    if not job or (job["user_id"] != user.id and user.role != "admin"):
        raise HTTPException(404, "Job not found (finished jobs are kept for an hour).")
    return {k: job[k] for k in ("id", "kind", "status", "result", "error", "code")}


@api_router.get("/apps/{app_id}/logs")
async def get_app_logs(
    app_id: int,
    lines: int = Query(default=2000, ge=1, le=10000),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Return the last *lines* lines of the app log (default 2000, max 10000)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    log_file = _app_log_file(app_row)
    if not log_file.exists():
        return {"content": ""}

    def _read_tail() -> dict:
        MAX_TAIL_BYTES = 512 * 1024
        try:
            stat = log_file.stat()
            with open(log_file, "rb") as lf:
                if stat.st_size > MAX_TAIL_BYTES:
                    lf.seek(-MAX_TAIL_BYTES, 2)
                    chunk = lf.read().decode("utf-8", errors="replace")
                    chunk = chunk[chunk.find("\n") + 1:]
                else:
                    chunk = lf.read().decode("utf-8", errors="replace")
            log_lines = chunk.splitlines()
            if len(log_lines) > lines:
                tail = "\n".join(log_lines[-lines:])
                return {"content": tail, "truncated": True, "total_lines": len(log_lines)}
            return {"content": chunk, "truncated": False, "total_lines": len(log_lines)}
        except Exception:
            return {"content": ""}

    return await asyncio.to_thread(_read_tail)


@api_router.delete("/apps/{app_id}/logs")
async def clear_app_logs(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    log_file = _app_log_file(app_row)
    if log_file.exists():
        await asyncio.to_thread(log_file.write_text, "", encoding="utf-8")
    return {"ok": True}


@api_router.get("/apps/{app_id}/scan-imports")
async def scan_imports(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Scan imported packages to discover dependencies missing from the active venv."""
    import ast as _ast

    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    root = Path(app_row.app_dir)
    venv_python = Path(app_row.venv_path) / "bin" / "python"

    installed: Set[str] = set()
    if venv_python.exists():
        proc = await asyncio.create_subprocess_exec(
            str(venv_python), "-m", "pip", "list", "--format=json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_app_tool_env(app_row),
            **_app_proc_kwargs(app_row),
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            stdout = b"[]"
        try:
            for pkg in json.loads(stdout):
                n = pkg["name"].lower()
                installed.add(n)
                installed.add(n.replace("-", "_"))
                installed.add(n.replace("_", "-"))
        except Exception:
            pass

    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()

    imported: Set[str] = set()
    for py_file in root.rglob("*.py"):
        parts = py_file.parts
        if "venv" in parts or "__pycache__" in parts:
            continue
        try:
            fileobj, _st = await asyncio.to_thread(_fs_open_file, root, py_file.relative_to(root).parts)
            with fileobj:
                text = (await asyncio.to_thread(fileobj.read)).decode("utf-8", errors="replace")
            tree = _ast.parse(text)
        except (AppPathError, OSError, SyntaxError, ValueError, RecursionError):
            # Not a plain file we can reach without following a link, or not
            # parseable Python (binary content raises ValueError, not SyntaxError).
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                if node.module and node.level == 0:
                    imported.add(node.module.split(".")[0])

    missing = sorted(
        name for name in imported
        if name
        and name not in stdlib
        and name not in installed
        and not name.startswith("_")
    )

    return {"missing": missing, "installed_count": len(installed)}


@api_router.get("/system/info")
async def system_info(_: User = Depends(get_current_user)):
    """Return the server's LAN IPv4 address."""
    ipv4 = await asyncio.to_thread(get_server_ipv4)
    return {"ipv4": ipv4}


@api_router.get("/system/ports")
async def system_ports(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """Return all currently listening TCP ports plus recommended free ports."""
    def _collect_listening() -> List[Dict]:
        listening: List[Dict] = []
        try:
            if psutil:
                for conn in _psutil_net_connections(kind="tcp"):
                    if conn.status == "LISTEN" and conn.laddr:
                        listening.append({"port": conn.laddr.port, "pid": conn.pid})
            else:
                for path in ("/proc/net/tcp", "/proc/net/tcp6"):
                    try:
                        lines = Path(path).read_text().splitlines()[1:]
                        for line in lines:
                            parts = line.split()
                            if len(parts) < 4 or parts[3] != "0A":
                                continue
                            port_hex = parts[1].split(":")[1]
                            listening.append({"port": int(port_hex, 16), "pid": None})
                    except Exception:
                        pass
        except Exception as exc:
            logging.warning(f"system_ports collect error: {exc}")
        return listening

    def _find_free_ports(used: Set[int], count: int = 12) -> List[int]:
        CANDIDATE_RANGES = [
            (3000, 3010), (4000, 4010), (5000, 5010),
            (8000, 8020), (8080, 8090), (8888, 8898),
            (9000, 9010), (9090, 9100),
        ]
        free: List[int] = []
        for start, end in CANDIDATE_RANGES:
            for port in range(start, end):
                if port in used or len(free) >= count:
                    continue
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.settimeout(0)
                        s.bind(("", port))
                        free.append(port)
                except OSError:
                    pass
        return free

    listening_raw, app_rows = await asyncio.gather(
        asyncio.to_thread(_collect_listening),
        asyncio.to_thread(lambda: session.exec(select(App)).all()),
    )

    seen: Set[int] = set()
    result = []
    for entry in sorted(listening_raw, key=lambda x: x["port"]):
        if entry["port"] not in seen:
            seen.add(entry["port"])
            result.append(entry)

    app_port_map: Dict[int, str] = {a.port: a.name for a in app_rows if a.port}
    for entry in result:
        entry["app_name"] = app_port_map.get(entry["port"])

    free_ports = await asyncio.to_thread(_find_free_ports, seen)

    return {"listening": result, "free": free_ports}


@api_router.get("/system/settings/{key}")
def get_system_setting(
    key: str,
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    setting = session.get(SystemSetting, key)
    if _is_secret_setting(key):
        # Write-only: a credential has no business travelling back to a
        # browser. The server uses it; the UI only needs to know it exists.
        return {"key": key, "value": "", "is_set": bool(setting and setting.value)}
    if not setting:
        return {"key": key, "value": ""}
    value = setting.value
    if key.startswith("cf_"):
        value = _decrypt_secret(value)
    return {"key": key, "value": value}


@api_router.put("/system/settings/{key}")
def set_system_setting(
    key: str,
    req: SystemSettingReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    _reject_reserved_setting(key)
    value, stored_value = _setting_for_storage(key, req.value)
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = stored_value
    else:
        setting = SystemSetting(key=key, value=stored_value)
    session.add(setting)
    session.commit()
    audit_log(admin.username, "system_setting_change", f"key={key}")
    return {"ok": True, "stored_length": len(value)}


class BatchSettingItem(BaseModel):
    key: str
    value: str


@api_router.post("/system/settings/batch")
def set_system_settings_batch(
    items: List[BatchSettingItem],
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Save multiple settings in a single DB transaction."""
    for item in items:
        _reject_reserved_setting(item.key)
    for item in items:
        _, stored_value = _setting_for_storage(item.key, item.value)
        setting = session.get(SystemSetting, item.key)
        if setting:
            setting.value = stored_value
        else:
            setting = SystemSetting(key=item.key, value=stored_value)
        session.add(setting)
    session.commit()
    audit_log(admin.username, "system_setting_change", f"batch keys={[i.key for i in items]}")
    return {"ok": True, "saved": len(items)}


@api_router.post("/system/cloudflare/test")
async def test_cf_credentials(
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Verify saved Cloudflare integration credentials and permissions."""
    import urllib.request as _ur
    import urllib.error  as _ue

    def _fetch(url: str, token: str):
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req  = _ur.Request(url, headers=hdrs)
        with _ur.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    creds = _get_cf_creds(session)
    token      = creds["cf_api_token"]
    account_id = creds["cf_account_id"]
    tunnel_id  = creds["cf_tunnel_id"]

    results = {}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}",
            token,
        )
        if data.get("success"):
            results["account"] = {"ok": True, "name": data["result"].get("name", account_id)}
        else:
            msgs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            results["account"] = {"ok": False, "error": msgs or "Unknown error"}
    except _ue.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            msgs = "; ".join(e.get("message", "") for e in json.loads(raw).get("errors", []))
        except Exception:
            msgs = raw[:200]
        results["account"] = {"ok": False, "error": f"HTTP {exc.code}: {msgs}"}
    except Exception as exc:
        results["account"] = {"ok": False, "error": str(exc)}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            token,
        )
        if data.get("success"):
            result      = data.get("result") or {}
            actual_id   = result.get("id", "")
            tunnel_name = result.get("name", tunnel_id)
            status      = result.get("status", "unknown")
            connections  = result.get("connections") or []
            active_conns = len([c for c in connections if c.get("is_pending_reconnect") is False])
            results["tunnel"] = {
                "ok":               True,
                "name":             tunnel_name,
                "id":               actual_id,
                "status":           status,
                "active_connections": active_conns,
                "message":          f"{tunnel_name} ({status}) — {active_conns} active connection(s)",
            }
            if active_conns == 0:
                results["tunnel"]["warning"] = (
                    "No active connections — the cloudflared connector may not be running. "
                    "Start cloudflared on your server or the tunnel will give Error 1016."
                )
        else:
            msgs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            results["tunnel"] = {"ok": False, "error": msgs or "Unknown error"}
    except _ue.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            msgs = "; ".join(e.get("message", "") for e in json.loads(raw).get("errors", []))
        except Exception:
            msgs = raw[:200]
        results["tunnel"] = {"ok": False, "error": f"HTTP {exc.code}: {msgs}"}
    except Exception as exc:
        results["tunnel"] = {"ok": False, "error": str(exc)}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/zones?account.id={account_id}&per_page=5",
            token,
        )
        if data.get("success"):
            zone_count = len(data.get("result", []))
            results["dns_zones"] = {
                "ok": True,
                "message": f"Found {zone_count} zone(s) — DNS CNAME records will be managed automatically.",
            }
        else:
            results["dns_zones"] = {
                "ok": False,
                "error": "Cannot list zones — add Zone:Read and DNS:Edit permissions to your API token for automatic DNS management.",
            }
    except _ue.HTTPError as exc:
        results["dns_zones"] = {
            "ok": False,
            "error": f"HTTP {exc.code}: Zone access denied. Add Zone:Read and DNS:Edit to your token.",
        }
    except Exception as exc:
        results["dns_zones"] = {"ok": False, "error": str(exc)}

    all_ok = all(v["ok"] for v in results.values())
    return {"ok": all_ok, "checks": results}


@api_router.get("/system/cloudflare/status")
def cf_status(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """Whether the Cloudflare integration is configured — what the tunnel UI
    needs to know, for everyone allowed to publish routes, without handing
    out the API token."""
    def _value(key: str) -> str:
        row = session.get(SystemSetting, key)
        return _sanitize_cf_value(_decrypt_secret(row.value)) if row and row.value else ""

    token, account, tunnel = _value("cf_api_token"), _value("cf_account_id"), _value("cf_tunnel_id")
    return {
        "configured": bool(token and account and tunnel),
        "token_set": bool(token),
        "account_id_hint": account[:8],
        "tunnel_id_hint": tunnel[:8],
    }


@api_router.get("/system/cloudflare/zones")
async def list_cf_zones(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """Return all active zones (domains) on the Cloudflare account."""
    creds      = _get_cf_creds(session)
    # _account_id = creds["cf_account_id"]
    api_token  = creds["cf_api_token"]

    zones: List[Dict[str, str]] = []
    page        = 1
    per_page    = 50

    async def _fetch_zones():
        nonlocal page
        while True:
            url = (
                f"https://api.cloudflare.com/client/v4/zones"
                f"?per_page={per_page}&page={page}&status=active"
            )
            try:
                data = await _cf_request("GET", url, api_token)
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    raise HTTPException(
                        403,
                        "Cloudflare API token lacks Zone:Read permission — cannot list domains. "
                        "Add Zone:Read and DNS:Edit to your token."
                    )
                raise

            result_list = data.get("result") or []
            for z in result_list:
                name = z.get("name", "")
                zid  = z.get("id",   "")
                if name and zid:
                    zones.append({"name": name, "id": zid})

            result_info = data.get("result_info") or {}
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages or page >= 10:
                break
            page += 1

    try:
        await asyncio.wait_for(_fetch_zones(), timeout=12.0)
    except asyncio.TimeoutError:
        logging.warning("Cloudflare zone list timed out — returning partial results.")
    except HTTPException:
        raise
    except Exception as exc:
        logging.error(f"CF zone list error: {exc}")
        if not zones:
            raise HTTPException(502, f"Cloudflare API error: {exc}")

    zones.sort(key=lambda z: z["name"])
    return {"ok": True, "zones": zones}


class GhClientIdReq(BaseModel):
    client_id: str


@api_router.put("/system/github/client-id")
def set_gh_client_id(
    req: GhClientIdReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    _gh_save(session, "gh_client_id", req.client_id.strip())
    session.commit()
    audit_log(admin.username, "system_setting_change", "key=gh_client_id")
    return {"ok": True}


@api_router.post("/system/github/device/start")
async def gh_device_start(
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Start a GitHub OAuth Device Flow authorization. The client polls
    /system/github/device/poll with the returned flow_id until it completes."""
    client_id = _gh_client_id(session)
    if not client_id:
        raise HTTPException(400, "Set a GitHub OAuth App Client ID first.")

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                _GH_DEVICE_CODE_URL,
                data={"client_id": client_id, "scope": "repo"},
                headers={"Accept": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Could not reach GitHub: {exc}")

    if "device_code" not in data:
        raise HTTPException(
            400,
            data.get("error_description") or data.get("error") or "GitHub rejected the request — check the Client ID.",
        )

    now = time.time()
    for fid in [f for f, v in _gh_device_flows.items() if now > v["expires_at"]]:
        _gh_device_flows.pop(fid, None)

    flow_id = secrets.token_hex(16)
    _gh_device_flows[flow_id] = {
        "device_code": data["device_code"],
        "client_id": client_id,
        "interval": data.get("interval", 5),
        "expires_at": now + data.get("expires_in", 900),
    }
    return {
        "flow_id": flow_id,
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri") or "https://github.com/login/device",
        "expires_in": data.get("expires_in", 900),
        "interval": data.get("interval", 5),
    }


class GhDevicePollReq(BaseModel):
    flow_id: str


@api_router.post("/system/github/device/poll")
async def gh_device_poll(
    req: GhDevicePollReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Poll GitHub for whether the user has approved the device authorization."""
    flow = _gh_device_flows.get(req.flow_id)
    if not flow:
        raise HTTPException(404, "Sign-in expired — start again.")
    if time.time() > flow["expires_at"]:
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, "Sign-in code expired — start again.")

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                _GH_TOKEN_URL,
                data={
                    "client_id": flow["client_id"],
                    "device_code": flow["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Could not reach GitHub: {exc}")

    error = data.get("error")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        flow["interval"] = data.get("interval", flow["interval"] + 5)
        return {"status": "pending", "interval": flow["interval"]}
    if error == "expired_token":
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, "Sign-in code expired — start again.")
    if error == "access_denied":
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, "Sign-in was cancelled.")
    if error:
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, data.get("error_description") or error)

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(502, "GitHub did not return an access token.")
    _gh_device_flows.pop(req.flow_id, None)

    gh_user: dict = {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(
                f"{_GH_API_BASE}/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            ) as resp:
                gh_user = await resp.json(content_type=None)
    except aiohttp.ClientError:
        pass

    username = gh_user.get("login", "")
    avatar = gh_user.get("avatar_url", "")

    _gh_save(session, "gh_token", access_token)
    _gh_save(session, "gh_username", username)
    _gh_save(session, "gh_avatar", avatar)
    session.commit()
    audit_log(admin.username, "github_connect", f"connected as {username}")
    return {"status": "complete", "username": username, "avatar_url": avatar}


@api_router.get("/system/github/status")
def gh_status(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return {
        "connected": bool(_gh_token(session)),
        "client_id_set": bool(_gh_client_id(session)),
        "username": _gh_setting(session, "gh_username"),
        "avatar_url": _gh_setting(session, "gh_avatar"),
    }


@api_router.get("/system/github/repos")
async def gh_list_repos(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """List repos the connected GitHub account can push to, newest-updated first."""
    token = _gh_token(session)
    if not token:
        raise HTTPException(400, "Not signed in to GitHub.")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    timeout = aiohttp.ClientTimeout(total=15)
    repos: List[Dict] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            for page in (1, 2, 3):
                url = f"{_GH_API_BASE}/user/repos?per_page=100&page={page}&sort=updated&affiliation=owner,collaborator,organization_member"
                async with http.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        break
                    batch = await resp.json(content_type=None)
                if not isinstance(batch, list) or not batch:
                    break
                for r in batch:
                    repos.append({
                        "full_name": r.get("full_name", ""),
                        "clone_url": r.get("clone_url", ""),
                        "private": bool(r.get("private")),
                        "default_branch": r.get("default_branch") or "main",
                    })
                if len(batch) < 100:
                    break
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Could not reach GitHub: {exc}")

    return {"repos": repos}


@api_router.post("/system/github/disconnect")
def gh_disconnect(
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    for key in ("gh_token", "gh_username", "gh_avatar"):
        setting = session.get(SystemSetting, key)
        if setting:
            session.delete(setting)
    session.commit()
    audit_log(admin.username, "github_disconnect", "")
    return {"ok": True}


@api_router.get("/apps/{app_id}/git/status")
async def git_status(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    return await _git_status_payload(app_row, session)


class GitCloneReq(BaseModel):
    url: str
    branch: Optional[str] = None


@api_router.post("/apps/{app_id}/git/clone")
async def git_clone(
    app_id: int,
    req: GitCloneReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Connect an app to a repository: init + fetch + checkout into its existing
    directory. Safe — git refuses (and we roll back) if any tracked file would
    overwrite something already there (e.g. logs/, venv/)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    app_dir = Path(app_row.app_dir)
    if await asyncio.to_thread((app_dir / ".git").exists):
        raise HTTPException(400, "This app is already connected to a repository.")

    url = req.url.strip()
    if not re.match(r"^https://[^\s@]+\.[^\s@]+/", url):
        raise HTTPException(400, "Only https:// repository URLs are supported.")
    branch = (req.branch or "").strip()
    if branch and not re.fullmatch(r"[A-Za-z0-9_./\-]+", branch):
        raise HTTPException(400, "Invalid branch name.")

    token = _gh_token(session)
    await asyncio.to_thread(_ensure_app_user, app_row)

    async def _job() -> dict:
        auth_cfg = _git_auth_config(token)
        target_branch = branch
        if not target_branch:
            out = await _run_git(app_row, [*auth_cfg, "ls-remote", "--symref", url, "HEAD"], timeout=30, token=token)
            m = re.search(r"ref:\s*refs/heads/(\S+)\s+HEAD", out)
            target_branch = m.group(1) if m else "main"

        await _run_git(app_row, ["init", "-q"], token=token)
        try:
            await _run_git(app_row, ["remote", "add", "origin", url], token=token)
            await _run_git(app_row, [*auth_cfg, "fetch", "--depth", "1", "origin", target_branch], timeout=180, token=token)
            await _run_git(app_row, ["checkout", "-b", target_branch, "--track", f"origin/{target_branch}"], token=token)
        except HTTPException:
            await asyncio.to_thread(shutil.rmtree, app_dir / ".git", ignore_errors=True)
            raise
        await asyncio.to_thread(_chown_app_dir, app_row)
        audit_log(user.username, "git_clone", f"app '{app_row.name}' (id={app_id}) <- {url}")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_clone", _job)


@api_router.post("/apps/{app_id}/git/fetch")
async def git_fetch(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    token = _gh_token(session)

    async def _job() -> dict:
        await _run_git(app_row, [*_git_auth_config(token), "fetch", "origin"], timeout=180, token=token)
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_fetch", _job)


@api_router.post("/apps/{app_id}/git/pull")
async def git_pull(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    token = _gh_token(session)

    async def _job() -> dict:
        await _run_git(app_row, [*_git_auth_config(token), "pull", "--ff-only", "origin"], timeout=180, token=token)
        await asyncio.to_thread(_chown_app_dir, app_row)
        audit_log(user.username, "git_pull", f"app '{app_row.name}' (id={app_id})")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_pull", _job)


class GitPushReq(BaseModel):
    force: bool = False


@api_router.post("/apps/{app_id}/git/push")
async def git_push(
    app_id: int,
    req: GitPushReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    token = _gh_token(session)

    async def _job() -> dict:
        args = [*_git_auth_config(token), "push"]
        if req.force:
            args.append("--force-with-lease")
        args.append("origin")
        await _run_git(app_row, args, timeout=180, token=token)
        audit_log(user.username, "git_push", f"app '{app_row.name}' (id={app_id}) force={req.force}")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_push", _job)


class GitCommitReq(BaseModel):
    message: str


@api_router.post("/apps/{app_id}/git/commit")
async def git_commit(
    app_id: int,
    req: GitCommitReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Commit message is required.")

    gh_username = _gh_setting(session, "gh_username")
    name = gh_username or user.username
    email = f"{gh_username}@users.noreply.github.com" if gh_username else f"{user.username}@venvy.local"

    async def _job() -> dict:
        await _run_git(app_row, ["add", "-A"], timeout=60)
        try:
            await _run_git(
                app_row,
                ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message],
                timeout=30,
            )
        except HTTPException as exc:
            if "nothing to commit" in str(exc.detail).lower():
                raise HTTPException(400, "Nothing to commit — working tree is clean.")
            raise
        audit_log(user.username, "git_commit", f"app '{app_row.name}' (id={app_id}): {message[:80]}")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_commit", _job)


@api_router.delete("/apps/{app_id}/git")
async def git_disconnect(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Unlink the repo (removes .git only — the working files are left in place)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    git_dir = Path(app_row.app_dir) / ".git"
    if not await asyncio.to_thread(git_dir.exists):
        raise HTTPException(400, "Not a git repository.")
    await asyncio.to_thread(shutil.rmtree, git_dir, ignore_errors=True)
    audit_log(user.username, "git_disconnect", f"app '{app_row.name}' (id={app_id})")
    return {"ok": True}


@api_router.get("/apps/{app_id}/tunnel")
def list_cf_routes(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    routes = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all()
    return {
        "routes": [
            {
                "hostname": route.hostname,
                "local_port": route.local_port,
                "published": bool(route.cf_route_id),
                "cf_route_id": route.cf_route_id,
            }
            for route in routes
        ]
    }


class CfPublishReq(BaseModel):
    hostname: str
    local_port: int


@api_router.get("/apps/{app_id}/tunnel/check")
async def check_cf_hostname(
    app_id: int,
    hostname: str = Query(...),
    local_port: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
):
    """Pre-flight check before publishing a hostname."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    clean = _clean_cf_hostname(hostname)

    existing_route = session.exec(
        select(CfRoute).where(CfRoute.hostname == clean)
    ).first()
    if existing_route and existing_route.app_id != app_id and existing_route.cf_route_id:
        other_app = session.get(App, existing_route.app_id)
        other_name = other_app.name if other_app else f"app #{existing_route.app_id}"
        return {
            "conflict":      True,
            "conflict_type": "local",
            "reason":        f"This hostname is already published by '{other_name}'. Publishing here will remove it from that app.",
            "owned_by":      other_name,
        }

    try:
        creds = _get_cf_creds(session)
        account_id = creds["cf_account_id"]
        tunnel_id  = creds["cf_tunnel_id"]
        api_token  = creds["cf_api_token"]

        cfg_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/cfd_tunnel/{tunnel_id}/configurations"
        )
        current_cfg = await _cf_request("GET", cfg_url, api_token)
        result_obj  = current_cfg.get("result") or {}
        ingress     = result_obj.get("config", {}).get("ingress") or []

        for rule in ingress:
            if rule.get("hostname") == clean:
                current_service = rule.get("service", "unknown")
                server_ip = get_server_ipv4()
                check_port = local_port or app_row.port
                expected_service = f"http://{server_ip}:{check_port}" if check_port else None
                expected_service_legacy = f"http://localhost:{check_port}" if check_port else None
                if expected_service and (current_service == expected_service or current_service == expected_service_legacy):
                    return {"conflict": False}
                return {
                    "conflict":      True,
                    "conflict_type": "remote",
                    "reason":        f"This hostname is already in the tunnel config pointing to '{current_service}'. Publishing will overwrite it.",
                    "owned_by":      None,
                }
    except HTTPException:
        pass

    return {"conflict": False}


@api_router.post("/apps/{app_id}/tunnel/publish")
async def publish_cf_route(
    app_id: int,
    req: CfPublishReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Publish (or republish) an app route via Cloudflare Tunnel."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not req.local_port:
        raise HTTPException(400, "Local port must be specified for Cloudflare tunnel mapping.")
    if not (1 <= req.local_port <= 65535):
        raise HTTPException(400, "Local port must be between 1 and 65535.")

    new_hostname = _clean_cf_hostname(req.hostname)

    creds      = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    tunnel_id  = creds["cf_tunnel_id"]
    api_token  = creds["cf_api_token"]

    cfg_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/cfd_tunnel/{tunnel_id}/configurations"
    )

    existing_route = session.exec(select(CfRoute).where(
        CfRoute.app_id == app_id,
        CfRoute.local_port == req.local_port
    )).first()
    old_hostname: Optional[str] = (
        existing_route.hostname
        if existing_route and existing_route.cf_route_id and existing_route.hostname
        else None
    )
    is_hostname_change = old_hostname is not None and old_hostname != new_hostname

    current_cfg = await _cf_request("GET", cfg_url, api_token)
    if not current_cfg.get("success"):
        errs = current_cfg.get("errors", [])
        raise HTTPException(502, f"Cloudflare returned failure: {errs}")

    result_obj = current_cfg.get("result") or {}
    ingress    = result_obj.get("config", {}).get("ingress") or []
    catch_all  = [r for r in ingress if not r.get("hostname")]

    hostnames_to_strip = {new_hostname}
    if is_hostname_change:
        hostnames_to_strip.add(old_hostname)
    named = [r for r in ingress if r.get("hostname") and r["hostname"] not in hostnames_to_strip]

    server_ip = get_server_ipv4()
    named.append({
        "hostname": new_hostname,
        "service":  f"http://{server_ip}:{req.local_port}",
    })
    new_ingress = named + (catch_all or [{"service": "http_status:404"}])

    put_resp = await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}})
    if not put_resp.get("success"):
        errs = put_resp.get("errors", [])
        raise HTTPException(502, f"Cloudflare tunnel update failed: {errs}")

    if is_hostname_change:
        try:
            old_zone_id = await _cf_find_zone_id(old_hostname, account_id, api_token)
            if old_zone_id:
                await _cf_delete_dns_cname(old_zone_id, old_hostname, api_token)
                logging.info(f"Removed old DNS CNAME: {old_hostname}")
        except Exception as exc:
            logging.warning(f"Could not delete old DNS CNAME for {old_hostname!r}: {exc}")

    try:
        new_zone_id = await _cf_find_zone_id(new_hostname, account_id, api_token)
    except HTTPException as exc:
        logging.warning(f"Zone lookup failed for {new_hostname!r}, rolling back ingress: {exc.detail}")
        try:
            rollback = [r for r in ingress if r.get("hostname") and r["hostname"] not in hostnames_to_strip]
            rollback += catch_all or [{"service": "http_status:404"}]
            await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": rollback}})
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise

    if not new_zone_id:
        logging.warning(f"Zone not found for {new_hostname!r}, rolling back ingress")
        try:
            rollback = [r for r in ingress if r.get("hostname") and r["hostname"] not in hostnames_to_strip]
            rollback += catch_all or [{"service": "http_status:404"}]
            await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": rollback}})
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise HTTPException(
            400,
            f"Domain '{new_hostname}' was not found in your Cloudflare account. "
            f"Make sure the root domain is added to Cloudflare and your API token "
            f"has Zone:Read + DNS:Edit permissions."
        )

    try:
        dns_record_id = await _cf_upsert_dns_cname(new_zone_id, new_hostname, tunnel_id, api_token)
    except HTTPException as exc:
        logging.warning(f"DNS upsert failed for {new_hostname!r}, rolling back ingress: {exc.detail}")
        try:
            rollback = [r for r in ingress if r.get("hostname") and r["hostname"] not in hostnames_to_strip]
            rollback += catch_all or [{"service": "http_status:404"}]
            await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": rollback}})
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise

    logging.info(f"DNS CNAME upserted: {new_hostname} → {tunnel_id}.cfargotunnel.com (record {dns_record_id})")

    conflicting = session.exec(
        select(CfRoute).where(CfRoute.hostname == new_hostname)
    ).all()
    for c in conflicting:
        if c.app_id != app_id:
            other_app = session.get(App, c.app_id)
            if other_app and other_app.external_url == f"https://{new_hostname}":
                other_app.external_url = None
                session.add(other_app)
            c.cf_route_id = None
            c.hostname    = ""
            session.add(c)
            logging.info(f"Cleared stale CfRoute ownership of {new_hostname!r} from app {c.app_id}")

    route = existing_route or CfRoute(app_id=app_id, local_port=req.local_port)
    route.hostname    = new_hostname
    route.cf_route_id = new_hostname
    session.add(route)
    app_row.external_url = f"https://{new_hostname}"
    session.add(app_row)
    session.commit()
    audit_log(user.username, "tunnel_publish", f"app '{app_row.name}' (id={app_id}) -> {new_hostname}")

    return {
        "ok":            True,
        "hostname":      new_hostname,
        "url":           f"https://{new_hostname}",
        "dns_record_id": dns_record_id,
    }


@api_router.delete("/apps/{app_id}/tunnel/publish")
async def unpublish_cf_route(
    app_id: int,
    local_port: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Remove public hostname route mapping from Cloudflare tunnel and DNS record."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    route = session.exec(select(CfRoute).where(
        CfRoute.app_id == app_id,
        CfRoute.local_port == local_port
    )).first()
    
    if not route or not route.cf_route_id:
        raise HTTPException(400, "No published route found for this app.")

    creds      = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    tunnel_id  = creds["cf_tunnel_id"]
    api_token  = creds["cf_api_token"]

    cfg_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/cfd_tunnel/{tunnel_id}/configurations"
    )

    hostname = route.hostname

    current_cfg = await _cf_request("GET", cfg_url, api_token)
    if not current_cfg.get("success"):
        errs = current_cfg.get("errors", [])
        raise HTTPException(502, f"Cloudflare returned failure fetching tunnel config: {errs}")

    result_obj  = current_cfg.get("result") or {}
    ingress     = result_obj.get("config", {}).get("ingress") or []
    named       = [r for r in ingress if r.get("hostname") and r["hostname"] != hostname]
    catch_all   = [r for r in ingress if not r.get("hostname")]
    new_ingress = named + (catch_all or [{"service": "http_status:404"}])

    put_resp = await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}})
    if not put_resp.get("success"):
        raise HTTPException(502, f"Cloudflare tunnel update failed: {put_resp.get('errors')}")

    try:
        zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
        if zone_id:
            await _cf_delete_dns_cname(zone_id, hostname, api_token)
            logging.info(f"DNS CNAME deleted for {hostname}")
        else:
            logging.warning(f"Zone not found for {hostname!r} during unpublish — DNS record may remain.")
    except Exception as exc:
        logging.warning(f"Could not delete DNS record for {hostname}: {exc}")

    route.cf_route_id = None
    route.hostname    = ""
    session.add(route)
    
    other_active = session.exec(select(CfRoute).where(
        CfRoute.app_id == app_id,
        CfRoute.cf_route_id != None
    )).first()
    
    # external_url may be the hostname that was just removed; point it at a
    # route that still exists rather than leaving a dead link behind.
    app_row.external_url = f"https://{other_active.hostname}" if other_active else None
    session.add(app_row)

    session.commit()
    audit_log(user.username, "tunnel_unpublish", f"app '{app_row.name}' (id={app_id})")
    return {"ok": True}


@app.websocket("/api/apps/{app_id}/terminal")
async def terminal_ws(
    websocket: WebSocket,
    app_id: int,
):
    # No Depends(get_session) here: a dependency session lives as long as the
    # socket, and after its first query it sits in an open read transaction.
    # Every open terminal then pinned one pooled connection (15 terminals
    # exhausted the pool and hung the whole API) and kept SQLite from
    # checkpointing its WAL. Sessions below are opened and closed per lookup.
    await websocket.accept()

    # This endpoint is registered directly on `app`, not on api_router, so it
    # doesn't pick up api_router's check_ip_allowlist dependency automatically
    # — every other /api/* route is IP-gated, but a shell into an app's
    # sandbox was not. Check it explicitly, first, before any session/auth
    # logic below.
    try:
        check_ip_allowlist(websocket)
    except HTTPException:
        await websocket.close(code=1008, reason="Access from this IP is not allowed.")
        return

    # WebSockets aren't covered by CORS, and a page on any sibling port of
    # this host (i.e. a hosted app) is "same-site" — so check that the
    # handshake really comes from the Venvy UI itself.
    origin = websocket.headers.get("origin", "")
    host = websocket.headers.get("host", "")
    origin_host = urllib.parse.urlsplit(origin).netloc if origin else ""
    if not origin_host or (origin_host != host and origin not in _ALLOWED_ORIGINS):
        await websocket.close(code=1008, reason="Cross-origin terminal connections are not allowed.")
        return

    # Browsers can't put custom headers on a WebSocket handshake, so the page
    # sends its X-Venvy-Key as the first message: "\x01" + key.
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except Exception:
        await websocket.close(code=1008, reason="Not authenticated.")
        return
    auth_key = first[1:] if first.startswith("\x01") else None
    with Session(engine) as session:
        ws_user = _authenticate(session, websocket.cookies.get("session"), auth_key)
        ws_user_id, ws_user_role = (ws_user.id, ws_user.role) if ws_user else (None, None)
    if ws_user_id is None:
        await websocket.close(code=1008, reason="Not authenticated.")
        return

    cookie_token = websocket.cookies.get("session")

    def _still_allowed() -> bool:
        with Session(engine) as s:
            u = _authenticate(s, cookie_token, auth_key)
            return bool(u and u.role in ("admin", "operator") and s.get(App, app_id))

    try:
        await terminal_manager.start_terminal(
            websocket, app_id, ws_user_id, ws_user_role, _still_allowed
        )
    except Exception as exc:
        logging.error(f"WS terminal error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------

app.include_router(api_auth.router)
app.include_router(api_users.router)
app.include_router(api_files.router)
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