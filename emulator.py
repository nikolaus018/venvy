"""
Venvy Emulator — Windows-compatible fake backend for UI development.

Serves the UI from web/ and responds to every API endpoint with realistic fake data.
No database, no processes, no Linux-only deps (no pty/fcntl/psutil required).

Usage:
    pip install fastapi uvicorn
    python emulator.py

Then open http://localhost:54783 in your browser.
Run it from a checkout: it serves web/ and vendor/ next to this script.
"""

import json
import random
import secrets
import time
import datetime
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Request, Response, HTTPException, Query, Form, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = int(os.getenv("PORT", "54783"))
HOST = "127.0.0.1"
ASSETS_DIR = Path(__file__).parent.resolve()
WEB_DIR = ASSETS_DIR / "web"

app = FastAPI(title="Venvy Emulator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

FAKE_SESSION_TOKEN = "emulator-session-token-abc123"
FAKE_CSRF_TOKEN    = "emulator-csrf-token-xyz789"

# Fake user
FAKE_USER = {
    "id": 1,
    "username": "admin",
    "password": "admin",
    "is_admin": True,
    "totp_enabled": False,
}

# Fake apps — mutable so start/stop/update work in the session
FAKE_APPS: Dict[int, dict] = {
    1: {
        "id": 1,
        "user_id": 1,
        "name": "FastAPI Demo",
        "slug": "fastapi-demo",
        "runtime": "python",
        "status": "running",
        "pid": 12345,
        "port": 8000,
        "manual_ports": "[8000]",
        "detected_ports": "[8000]",
        "entrypoint": "main.py",
        "start_on_boot": True,
        "restart_policy": "always",
        "last_exit_code": None,
        "last_start_time": (datetime.datetime.utcnow() - datetime.timedelta(hours=2)).isoformat(),
        "last_stop_time": None,
        "last_manually_stopped_at": None,
        "venv_path": "C:\\venvy\\apps\\1\\fastapi-demo\\venv",
        "app_dir": "C:\\venvy\\apps\\1\\fastapi-demo",
        "env_json": '{"DEBUG":"true","HOST":"0.0.0.0"}',
        "external_url": "https://fastapi-demo.example.com",
        "healthcheck_path": "/health",
        "url_source_pref": "local",
        "created_at": (datetime.datetime.utcnow() - datetime.timedelta(days=10)).isoformat(),
        "updated_at": (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).isoformat(),
        "local_url": "http://127.0.0.1:8000",
        "access_url": "http://127.0.0.1:8000",
        "url_source": "local",
        "metrics": {"cpu": 2.3, "memory": 45_000_000},
        "memory_limit_mb": 512,
        "cpu_limit_percent": 50,
        "resource_limits_enforced": False,
    },
    2: {
        "id": 2,
        "user_id": 1,
        "name": "Data Pipeline",
        "slug": "data-pipeline",
        "runtime": "python",
        "status": "stopped",
        "pid": None,
        "port": 8001,
        "manual_ports": "[]",
        "detected_ports": "[8001]",
        "entrypoint": "pipeline.py",
        "start_on_boot": False,
        "restart_policy": "on-failure",
        "last_exit_code": 0,
        "last_start_time": (datetime.datetime.utcnow() - datetime.timedelta(days=1)).isoformat(),
        "last_stop_time": (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).isoformat(),
        "last_manually_stopped_at": None,
        "venv_path": "C:\\venvy\\apps\\1\\data-pipeline\\venv",
        "app_dir": "C:\\venvy\\apps\\1\\data-pipeline",
        "env_json": '{"BATCH_SIZE":"100"}',
        "external_url": None,
        "healthcheck_path": None,
        "url_source_pref": "local",
        "created_at": (datetime.datetime.utcnow() - datetime.timedelta(days=5)).isoformat(),
        "updated_at": (datetime.datetime.utcnow() - datetime.timedelta(hours=5)).isoformat(),
        "local_url": "http://127.0.0.1:8001",
        "access_url": "http://127.0.0.1:8001",
        "url_source": "local",
        "metrics": {"cpu": 0.0, "memory": 0},
        "memory_limit_mb": None,
        "cpu_limit_percent": 100,
        "resource_limits_enforced": False,
    },
    3: {
        "id": 3,
        "user_id": 1,
        "name": "Rust Service",
        "slug": "rust-service",
        "runtime": "rust",
        "status": "running",
        "pid": 55432,
        "port": 9000,
        "manual_ports": "[9000]",
        "detected_ports": "[9000]",
        "entrypoint": "src/main.rs",
        "start_on_boot": True,
        "restart_policy": "always",
        "last_exit_code": None,
        "last_start_time": (datetime.datetime.utcnow() - datetime.timedelta(minutes=30)).isoformat(),
        "last_stop_time": None,
        "last_manually_stopped_at": None,
        "venv_path": "",
        "app_dir": "C:\\venvy\\apps\\1\\rust-service",
        "env_json": "{}",
        "external_url": "https://rust-service.example.com",
        "healthcheck_path": "/ping",
        "url_source_pref": "public",
        "created_at": (datetime.datetime.utcnow() - datetime.timedelta(days=3)).isoformat(),
        "updated_at": (datetime.datetime.utcnow() - datetime.timedelta(minutes=30)).isoformat(),
        "local_url": "http://127.0.0.1:9000",
        "access_url": "https://rust-service.example.com",
        "url_source": "cloudflare",
        "metrics": {"cpu": 0.8, "memory": 12_000_000},
        "memory_limit_mb": 1024,
        "cpu_limit_percent": 200,
        "resource_limits_enforced": False,
    },
}

NEXT_APP_ID = 4  # auto-increment counter

FAKE_SETTINGS: Dict[str, str] = {
    "cf_api_token": "",
    "cf_account_id": "",
    "cf_tunnel_id": "",
}

FAKE_PACKAGES = {
    1: [
        {"name": "fastapi", "version": "0.111.0"},
        {"name": "uvicorn", "version": "0.29.0"},
        {"name": "pydantic", "version": "2.7.1"},
        {"name": "sqlalchemy", "version": "2.0.30"},
        {"name": "requests", "version": "2.31.0"},
        {"name": "httpx", "version": "0.27.0"},
    ],
    2: [
        {"name": "pandas", "version": "2.2.2"},
        {"name": "numpy", "version": "1.26.4"},
        {"name": "boto3", "version": "1.34.77"},
    ],
    3: [
        {"name": "tokio", "version": "1.37"},
        {"name": "serde", "version": "1.0"},
        {"name": "axum", "version": "0.7"},
    ],
}

FAKE_FILE_TREE = {
    1: [
        {"name": "main.py", "path": "main.py", "is_dir": False, "size": 4200},
        {"name": "models.py", "path": "models.py", "is_dir": False, "size": 1800},
        {"name": "routes", "path": "routes", "is_dir": True, "size": 0},
        {"name": "requirements.txt", "path": "requirements.txt", "is_dir": False, "size": 120},
        {"name": "config.json", "path": "config.json", "is_dir": False, "size": 340},
    ],
    2: [
        {"name": "pipeline.py", "path": "pipeline.py", "is_dir": False, "size": 3100},
        {"name": "utils", "path": "utils", "is_dir": True, "size": 0},
        {"name": "requirements.txt", "path": "requirements.txt", "is_dir": False, "size": 80},
    ],
    3: [
        {"name": "src", "path": "src", "is_dir": True, "size": 0},
        {"name": "Cargo.toml", "path": "Cargo.toml", "is_dir": False, "size": 640},
        {"name": "Cargo.lock", "path": "Cargo.lock", "is_dir": False, "size": 12400},
    ],
}

SAMPLE_LOG = """\
2024-06-01 12:00:01 INFO  Starting application...
2024-06-01 12:00:02 INFO  Loading configuration from config.json
2024-06-01 12:00:02 INFO  Database connection established
2024-06-01 12:00:03 INFO  Server listening on 0.0.0.0:8000
2024-06-01 12:00:05 INFO  GET /health 200 1ms
2024-06-01 12:00:10 INFO  GET /api/users 200 4ms
2024-06-01 12:00:15 INFO  POST /api/items 201 12ms
2024-06-01 12:00:20 WARNING Rate limit reached for IP 192.168.1.42
2024-06-01 12:00:25 INFO  GET /api/items?page=2 200 6ms
2024-06-01 12:00:30 ERROR Failed to connect to cache — retrying in 5s
2024-06-01 12:00:35 INFO  Cache reconnected successfully
2024-06-01 12:00:40 INFO  Background task: processed 142 items
2024-06-01 12:00:50 INFO  GET /health 200 1ms
2024-06-01 12:01:00 INFO  Scheduled job completed in 0.83s
"""

TOTP_PENDING_SECRET: Optional[str] = None

# ---------------------------------------------------------------------------
# Middleware — auto-set cookies
# ---------------------------------------------------------------------------

@app.middleware("http")
async def cookie_middleware(request: Request, call_next):
    response = await call_next(request)
    # Always ensure CSRF cookie is present
    if "csrf_token" not in request.cookies:
        response.set_cookie("csrf_token", FAKE_CSRF_TOKEN, httponly=False, samesite="lax", path="/")
    return response

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _check_auth(request: Request):
    """Minimal auth check — pass if the session cookie is present."""
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not authenticated.")

FAKE_AUTH_KEY = "emulator-auth-key"
FAKE_JOBS: Dict[str, dict] = {}

def _job(result: Any) -> dict:
    """Mimic the real server's background jobs: return {job_id}, already done."""
    job_id = secrets.token_hex(16)
    FAKE_JOBS[job_id] = {"id": job_id, "kind": "emulated", "status": "done", "result": result, "error": None, "code": None}
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    _check_auth(request)
    if job_id not in FAKE_JOBS:
        raise HTTPException(404, "Job not found.")
    return FAKE_JOBS[job_id]

@app.post("/api/auth/download-ticket")
def download_ticket(request: Request):
    _check_auth(request)
    return {"ticket": secrets.token_urlsafe(24)}

def _app_or_404(app_id: int) -> dict:
    if app_id not in FAKE_APPS:
        raise HTTPException(404, "App not found.")
    return FAKE_APPS[app_id]

def _now_iso():
    return datetime.datetime.utcnow().isoformat()

# ---------------------------------------------------------------------------
# Static — serve index.html
# ---------------------------------------------------------------------------

@app.get("/vendor/{name}")
def serve_vendor(name: str):
    path = ASSETS_DIR / "vendor" / Path(name).name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/javascript")

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=WEB_DIR / "static", check_dir=False), name="static")

@app.get("/sw.js")
def serve_service_worker():
    path = WEB_DIR / "sw.js"
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

@app.get("/")
async def serve_spa():
    html = WEB_DIR / "index.html"
    if html.exists():
        from fastapi.responses import HTMLResponse
        from venvy.webui import render_index   # stdlib-only; expands web/partials includes
        return HTMLResponse(render_index(WEB_DIR))
    return PlainTextResponse("web/index.html not found next to emulator.py.", status_code=404)

_BOOT = time.time()


@app.get("/api/health")
def health_check():
    return {"ok": True, "status": "healthy", "uptime_seconds": int(time.time() - _BOOT)}

# ---------------------------------------------------------------------------
# Auth — /api/auth/*
# ---------------------------------------------------------------------------

@app.get("/api/auth/status")
def auth_status(request: Request):
    token = request.cookies.get("session")
    authenticated = token == FAKE_SESSION_TOKEN and request.headers.get("x-venvy-key") == FAKE_AUTH_KEY
    return {
        "user_exists": True,
        "authenticated": authenticated,
        "username": FAKE_USER["username"] if authenticated else None,
        "is_admin": FAKE_USER["is_admin"] if authenticated else False,
        "role": ("admin" if FAKE_USER["is_admin"] else "operator") if authenticated else None,
        "totp_enabled": FAKE_USER["totp_enabled"] if authenticated else False,
    }

class LoginReq(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None

@app.post("/api/auth/login")
def login(payload: LoginReq, response: Response):
    if payload.username != FAKE_USER["username"] or payload.password != FAKE_USER["password"]:
        raise HTTPException(401, "Invalid credentials.")
    response.set_cookie("session", FAKE_SESSION_TOKEN, httponly=True, path="/", samesite="strict")
    return {"ok": True, "username": FAKE_USER["username"], "is_admin": FAKE_USER["is_admin"], "auth_key": FAKE_AUTH_KEY}

class SetupReq(BaseModel):
    username: str
    password: str
    setup_code: str = ""

@app.post("/api/auth/setup")
def auth_setup(payload: SetupReq, response: Response):
    response.set_cookie("session", FAKE_SESSION_TOKEN, httponly=True, path="/", samesite="strict")
    return {"ok": True, "username": payload.username, "is_admin": True, "auth_key": FAKE_AUTH_KEY}

@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("session", path="/")
    return {"ok": True}

class PwdReq(BaseModel):
    current_password: str
    password: str
    password_confirm: str

@app.put("/api/auth/password")
def change_password(req: PwdReq, request: Request):
    _check_auth(request)
    if req.current_password != FAKE_USER["password"]:
        raise HTTPException(401, "Current password is incorrect.")
    FAKE_USER["password"] = req.password
    return {"ok": True}

# TOTP
@app.get("/api/auth/totp/setup")
def totp_setup_begin(request: Request):
    global TOTP_PENDING_SECRET
    _check_auth(request)
    TOTP_PENDING_SECRET = "JBSWY3DPEHPK3PXP"  # fake base32 secret
    uri = f"otpauth://totp/Venvy:admin?secret={TOTP_PENDING_SECRET}&issuer=Venvy"
    return {"ok": True, "secret": TOTP_PENDING_SECRET, "uri": uri}

class TotpSetupConfirmReq(BaseModel):
    totp_code: str

@app.post("/api/auth/totp/setup/confirm")
def totp_setup_confirm(req: TotpSetupConfirmReq, request: Request):
    _check_auth(request)
    FAKE_USER["totp_enabled"] = True
    return {"ok": True}

class TotpDisableReq(BaseModel):
    password: str

@app.post("/api/auth/totp/disable")
def totp_disable(req: TotpDisableReq, request: Request):
    _check_auth(request)
    FAKE_USER["totp_enabled"] = False
    return {"ok": True}

# ---------------------------------------------------------------------------
# Users — /api/users
# ---------------------------------------------------------------------------

@app.get("/api/users")
def list_users(request: Request):
    _check_auth(request)
    return [{"id": 1, "username": "admin", "role": "admin", "all_apps": True, "app_ids": []},
            {"id": 2, "username": "sam", "role": "operator", "all_apps": False, "app_ids": [1]}]

class UserReq(BaseModel):
    username: str
    password: str

@app.post("/api/users")
def create_user(req: UserReq, request: Request):
    _check_auth(request)
    return {"ok": True}

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, request: Request):
    _check_auth(request)
    if user_id == 1:
        raise HTTPException(400, "Cannot delete yourself.")
    return {"ok": True}

# ---------------------------------------------------------------------------
# Apps — /api/apps
# ---------------------------------------------------------------------------

@app.get("/api/apps")
def list_apps(request: Request):
    _check_auth(request)
    # Slightly randomize CPU/memory for running apps to feel live
    apps = list(FAKE_APPS.values())
    for a in apps:
        if a["status"] == "running":
            a["metrics"] = {
                "cpu": round(random.uniform(0.5, 8.0), 1),
                "memory": random.randint(20_000_000, 120_000_000),
            }
    return apps

@app.post("/api/apps/upload")
async def upload_app(
    request: Request,
    name: str = Form(...),
    runtime: str = Form("python"),
    entrypoint: str = Form(""),
    start_on_boot: bool = Form(False),
    restart_policy: str = Form("never"),
    files: Optional[List[UploadFile]] = File(None),
    requirements: Optional[UploadFile] = File(None),
):
    global NEXT_APP_ID
    _check_auth(request)
    if runtime not in {r["key"] for r in FAKE_RUNTIMES}:
        raise HTTPException(400, "unknown runtime")
    app_id = NEXT_APP_ID
    NEXT_APP_ID += 1
    slug = name.lower().replace(" ", "-")
    new_app = {
        "id": app_id,
        "user_id": 1,
        "name": name,
        "slug": slug,
        "runtime": runtime,
        "status": "stopped",
        "pid": None,
        "port": None,
        "manual_ports": "[]",
        "detected_ports": "[]",
        "entrypoint": entrypoint or next(r["entry_default"] for r in FAKE_RUNTIMES if r["key"] == runtime),
        "start_on_boot": start_on_boot,
        "restart_policy": restart_policy,
        "last_exit_code": None,
        "last_start_time": None,
        "last_stop_time": None,
        "last_manually_stopped_at": None,
        "venv_path": f"C:\\venvy\\apps\\1\\{slug}\\venv",
        "app_dir": f"C:\\venvy\\apps\\1\\{slug}",
        "env_json": "{}",
        "external_url": None,
        "healthcheck_path": None,
        "url_source_pref": "local",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "local_url": None,
        "access_url": None,
        "url_source": None,
        "metrics": {"cpu": 0.0, "memory": 0},
        "memory_limit_mb": None,
        "cpu_limit_percent": None,
        "resource_limits_enforced": False,
    }
    FAKE_APPS[app_id] = new_app
    FAKE_PACKAGES[app_id] = []
    FAKE_FILE_TREE[app_id] = [
        {"name": entrypoint or "main.py", "path": entrypoint or "main.py", "is_dir": False, "size": 0},
        {"name": "requirements.txt", "path": "requirements.txt", "is_dir": False, "size": 0},
    ]
    return {**_job({**new_app, "skipped_files": []}), "app_id": app_id, "skipped_files": []}

@app.post("/api/apps/{app_id}/control")
async def control_app(app_id: int, action: str = Query(...), request: Request = None):
    _check_auth(request)
    a = _app_or_404(app_id)

    if action == "start":
        a["status"] = "running"
        a["pid"] = random.randint(10000, 99999)
        a["last_start_time"] = _now_iso()
        a["last_exit_code"] = None
        if not a.get("port"):
            a["port"] = random.choice([8080, 5000, 3000, 4000])
        a["local_url"] = f"http://127.0.0.1:{a['port']}"
        a["access_url"] = a["local_url"]
        a["metrics"] = {"cpu": 1.2, "memory": 35_000_000}
    elif action == "stop":
        a["status"] = "stopped"
        a["pid"] = None
        a["last_stop_time"] = _now_iso()
        a["last_exit_code"] = 0
        a["metrics"] = {"cpu": 0.0, "memory": 0}
    elif action == "restart":
        a["status"] = "running"
        a["pid"] = random.randint(10000, 99999)
        a["last_start_time"] = _now_iso()
        a["last_exit_code"] = None
        a["metrics"] = {"cpu": 0.5, "memory": 30_000_000}
    else:
        raise HTTPException(400, "Invalid action.")

    return {"status": a["status"], "queued": True}

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
    healthcheck_path: Optional[str] = None
    health_interval: Optional[int] = None
    health_failures: Optional[int] = None
    health_restart: Optional[bool] = None
    domain: Optional[str] = None
    disk_limit_mb: Optional[int] = None

@app.put("/api/apps/{app_id}")
async def update_app(app_id: int, req: AppUpdateReq, request: Request):
    _check_auth(request)
    a = _app_or_404(app_id)
    if req.name is not None:
        a["name"] = req.name
    if req.start_on_boot is not None:
        a["start_on_boot"] = req.start_on_boot
    if req.restart_policy is not None:
        a["restart_policy"] = req.restart_policy
    if req.runtime is not None:
        if req.runtime not in {r["key"] for r in FAKE_RUNTIMES}:
            raise HTTPException(400, "unknown runtime")
        a["runtime"] = req.runtime
    if req.port is not None:
        a["port"] = req.port if req.port != 0 else None
    if req.manual_ports is not None:
        a["manual_ports"] = json.dumps(req.manual_ports)
        a["port"] = req.manual_ports[0] if req.manual_ports else None
    if req.env_vars is not None:
        a["env_json"] = json.dumps(req.env_vars)
    if req.entrypoint is not None:
        a["entrypoint"] = req.entrypoint
    if req.url_source_pref is not None:
        a["url_source_pref"] = req.url_source_pref
    if req.memory_limit_mb is not None:
        if req.memory_limit_mb != 0 and not (16 <= req.memory_limit_mb <= 1_048_576):
            raise HTTPException(400, "memory_limit_mb must be between 16 and 1048576 (or 0 for unlimited).")
        a["memory_limit_mb"] = None if req.memory_limit_mb == 0 else req.memory_limit_mb
    if req.cpu_limit_percent is not None:
        if req.cpu_limit_percent != 0 and not (1 <= req.cpu_limit_percent <= 10000):
            raise HTTPException(400, "cpu_limit_percent must be between 1 and 10000 (or 0 for unlimited).")
        a["cpu_limit_percent"] = None if req.cpu_limit_percent == 0 else req.cpu_limit_percent
    for field in ("healthcheck_path", "health_interval", "health_failures", "health_restart", "domain", "disk_limit_mb"):
        value = getattr(req, field)
        if value is not None:
            a[field] = (value or None) if field in ("healthcheck_path", "domain", "disk_limit_mb") else value
    a["updated_at"] = _now_iso()
    return a

@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    del FAKE_APPS[app_id]
    FAKE_PACKAGES.pop(app_id, None)
    FAKE_FILE_TREE.pop(app_id, None)
    return {"ok": True, "warnings": []}

@app.get("/api/apps/{app_id}/stats")
def app_stats(app_id: int, request: Request):
    _check_auth(request)
    a = _app_or_404(app_id)
    if a["status"] == "running":
        return {
            "cpu": round(random.uniform(0.5, 10.0), 2),
            "memory": round(random.uniform(20.0, 150.0), 2),
            "uptime": random.randint(300, 86400),
        }
    return {"cpu": 0.0, "memory": 0.0, "uptime": 0.0}

# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

@app.get("/api/apps/{app_id}/logs")
async def get_app_logs(app_id: int, lines: int = Query(default=2000, ge=1, le=10000), request: Request = None):
    _check_auth(request)
    _app_or_404(app_id)
    # Generate some fake log lines
    log_lines = SAMPLE_LOG.strip().splitlines()
    # Append a few timestamped live lines
    for i in range(8):
        ts = (datetime.datetime.utcnow() - datetime.timedelta(seconds=i * 10)).strftime("%Y-%m-%d %H:%M:%S")
        log_lines.append(f"{ts} INFO  [emulator] heartbeat ping #{random.randint(100, 9999)}")
    content = "\n".join(log_lines[-lines:])
    return {"content": content, "truncated": False, "total_lines": len(log_lines)}

@app.delete("/api/apps/{app_id}/logs")
async def clear_app_logs(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    return {"ok": True}

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

@app.get("/api/apps/{app_id}/files/tree")
def file_tree(app_id: int, path: str = "", request: Request = None):
    _check_auth(request)
    _app_or_404(app_id)
    tree = FAKE_FILE_TREE.get(app_id, [])
    if path:
        # Pretend any sub-directory has a couple of files
        return [
            {"name": "index.py", "path": f"{path}/index.py", "is_dir": False, "size": 880},
            {"name": "helpers.py", "path": f"{path}/helpers.py", "is_dir": False, "size": 430},
        ]
    return tree

FAKE_FILE_CONTENTS: Dict[str, str] = {
    "main.py": '# FastAPI Demo App\nfrom fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get("/")\ndef root():\n    return {"hello": "world"}\n\n@app.get("/health")\ndef health():\n    return {"status": "ok"}\n',
    "requirements.txt": "fastapi==0.111.0\nuvicorn==0.29.0\npydantic==2.7.1\n",
    "config.json": '{\n  "debug": true,\n  "host": "0.0.0.0",\n  "port": 8000\n}\n',
    "Cargo.toml": '[package]\nname = "rust-service"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\ntokio = { version = "1", features = ["full"] }\naxum = "0.7"\nserde = { version = "1", features = ["derive"] }\n',
    "pipeline.py": "# Data Pipeline\nimport pandas as pd\n\ndef run():\n    df = pd.read_csv('data.csv')\n    print(f'Loaded {len(df)} rows')\n    # ... processing ...\n    return df\n\nif __name__ == '__main__':\n    run()\n",
}

@app.get("/api/apps/{app_id}/files/download")
async def download_file(app_id: int, path: str, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    filename = Path(path).name
    content = FAKE_FILE_CONTENTS.get(filename, f"# {filename}\n# (emulated file content)\n")
    from fastapi.responses import Response as R
    return R(
        content=content.encode(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

class FileOpReq(BaseModel):
    operation: str
    path: str
    new_path: Optional[str] = None
    content: Optional[str] = None

@app.post("/api/apps/{app_id}/files/op")
async def file_operations(app_id: int, req: FileOpReq, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if req.operation == "save" and req.content is not None:
        FAKE_FILE_CONTENTS[Path(req.path).name] = req.content
    return {"ok": True}

@app.post("/api/apps/{app_id}/files/upload")
async def upload_file_to_path(
    app_id: int,
    request: Request,
    path: str = Form(""),
    files: List[UploadFile] = File(...),
):
    _check_auth(request)
    _app_or_404(app_id)
    return {"ok": True, "skipped": []}

_FAKE_UPLOAD_PARTS: Dict[str, int] = {}

@app.post("/api/apps/{app_id}/files/upload/chunk")
async def upload_file_chunk(
    app_id: int,
    request: Request,
    upload_id: str = Form(...),
    path: str = Form(""),
    rel_path: str = Form(...),
    offset: int = Form(...),
    total: int = Form(...),
    chunk: UploadFile = File(...),
):
    _check_auth(request)
    _app_or_404(app_id)
    have = _FAKE_UPLOAD_PARTS.get(upload_id, 0)
    if offset != 0 and offset != have:
        raise HTTPException(409, {"message": "Offset mismatch.", "received": have})
    received = offset + len(await chunk.read())
    if received >= total:
        _FAKE_UPLOAD_PARTS.pop(upload_id, None)
        return {"ok": True, "received": received, "done": True}
    _FAKE_UPLOAD_PARTS[upload_id] = received
    return {"ok": True, "received": received, "done": False}

@app.delete("/api/apps/{app_id}/files/upload/{upload_id}")
async def abort_file_upload(app_id: int, upload_id: str, request: Request):
    _check_auth(request)
    _FAKE_UPLOAD_PARTS.pop(upload_id, None)
    return {"ok": True}

# The same table as venvy/runtimes.py (every tool pretended installed). tests/t_small.py checks the keys stay in step.
FAKE_RUNTIMES = [
  {
    "key": "python",
    "versions": ["3.11", "3.12"],
    "label": "Python 3 (Venv)",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y python3 python3-venv",
    "entry": "required",
    "entry_default": "main.py",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "main.py",
    "entry_help": "The Python file that launches your app (e.g. main.py). Upload files first if the app has none yet.",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Package name, e.g. numpy==1.26",
    "can_sync": False
  },
  {
    "key": "node",
    "label": "Node.js (npm)",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y nodejs npm   (or re-run the installer with --with-node)",
    "entry": "optional",
    "entry_default": "",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "server.js (blank = npm start)",
    "entry_help": "The file that launches your app (e.g. server.js). Leave it blank to run the \"start\" script of package.json (npm start).",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Package name, e.g. express@4",
    "can_sync": True
  },
  {
    "key": "bun",
    "label": "Bun",
    "installed": True,
    "packages_installed": True,
    "install_hint": "re-run the installer with --with-bun   (https://bun.sh)",
    "entry": "optional",
    "entry_default": "",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "index.ts (blank = bun run start)",
    "entry_help": "The file that launches your app (e.g. index.ts). Leave it blank to run the \"start\" script of package.json.",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Package name, e.g. hono@4",
    "can_sync": True
  },
  {
    "key": "deno",
    "label": "Deno",
    "installed": True,
    "packages_installed": True,
    "install_hint": "re-run the installer with --with-deno   (https://deno.com)",
    "entry": "optional",
    "entry_default": "",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "main.ts (blank = deno task start)",
    "entry_help": "The file that launches your app (e.g. main.ts), run with all permissions. Leave it blank to run the \"start\" task of deno.json.",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Package, e.g. jsr:@std/path or npm:express",
    "can_sync": True
  },
  {
    "key": "rust",
    "label": "Rust (Cargo)",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y cargo rustc build-essential   (or re-run the installer with --with-rust)",
    "entry": "fixed",
    "entry_default": "main.rs",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "main.rs",
    "entry_help": "Rust apps always start with cargo run.",
    "packages": "list",
    "pkg_help": "",
    "pkg_example": "",
    "can_sync": False
  },
  {
    "key": "go",
    "label": "Go",
    "installed": True,
    "packages_installed": True,
    "install_hint": "re-run the installer with --with-go   (https://go.dev/dl)",
    "entry": "fixed",
    "entry_default": "main.go",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "main.go",
    "entry_help": "Go apps always start with go run . — the main package in the app's folder.",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Module, e.g. github.com/gin-gonic/gin@v1.10.0",
    "can_sync": True
  },
  {
    "key": "php",
    "label": "PHP",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y php-cli composer unzip   (or re-run the installer with --with-php)",
    "entry": "optional",
    "entry_default": "",
    "entry_label": "Router Script",
    "entry_placeholder": "blank = serve the folder",
    "entry_help": "Leave blank to serve the app's folder (or its public/ folder) with PHP's built-in web server; unknown paths go to index.php. Or name a router script (e.g. index.php) to handle every request yourself.",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Package, e.g. monolog/monolog:^3",
    "can_sync": True
  },
  {
    "key": "ruby",
    "label": "Ruby (Bundler)",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y ruby ruby-bundler build-essential   (or re-run the installer with --with-ruby)",
    "entry": "required",
    "entry_default": "app.rb",
    "entry_label": "Entrypoint File",
    "entry_placeholder": "app.rb or config.ru",
    "entry_help": "The Ruby file that launches your app (e.g. app.rb). A config.ru is started with rackup. With a Gemfile the app runs under bundle exec.",
    "packages": "manage",
    "pkg_help": "",
    "pkg_example": "Gem name, e.g. sinatra@4.0",
    "can_sync": True
  },
  {
    "key": "java",
    "label": "Java (JAR)",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y default-jre-headless   (or re-run the installer with --with-java)",
    "entry": "required",
    "entry_default": "app.jar",
    "entry_label": "JAR File",
    "entry_placeholder": "app.jar",
    "entry_help": "The executable JAR that launches your app, run with java -jar. The port is passed as PORT and SERVER_PORT (Spring Boot picks that up).",
    "packages": "none",
    "pkg_help": "Java apps are a single JAR with its dependencies bundled — there is nothing to install here.",
    "pkg_example": "",
    "can_sync": False
  },
  {
    "key": "static",
    "label": "Static site",
    "installed": True,
    "packages_installed": True,
    "install_hint": "sudo apt-get install -y python3",
    "entry": "optional",
    "entry_default": "",
    "entry_label": "Site Folder",
    "entry_placeholder": "blank = public, dist, build, site or the app folder",
    "entry_help": "The folder that holds the site. Blank picks the first of public, dist, build, site, www or html that exists, else the app's own folder. index.html is the home page; /about serves about.html; a 404.html is used for unknown paths.",
    "packages": "none",
    "pkg_help": "A static site has no packages.",
    "pkg_example": "",
    "can_sync": False
  }
]

@app.get("/api/runtimes")
async def list_runtimes(request: Request):
    _check_auth(request)
    return FAKE_RUNTIMES

# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

@app.get("/api/apps/{app_id}/packages")
async def list_packages(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    return FAKE_PACKAGES.get(app_id, [])

class PackageReq(BaseModel):
    name: str

@app.post("/api/apps/{app_id}/packages")
async def install_package(app_id: int, req: PackageReq, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    # Simulate a 1.5s install delay (non-blocking)
    import asyncio
    await asyncio.sleep(1.5)
    pkgs = FAKE_PACKAGES.setdefault(app_id, [])
    if not any(p["name"] == req.name for p in pkgs):
        pkgs.append({"name": req.name, "version": "latest"})
    return _job({"ok": True})

@app.post("/api/apps/{app_id}/packages/sync")
async def sync_packages(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    import asyncio
    await asyncio.sleep(1.5)
    return _job({"ok": True})

@app.delete("/api/apps/{app_id}/packages/{name:path}")
async def uninstall_package(app_id: int, name: str, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    FAKE_PACKAGES[app_id] = [p for p in FAKE_PACKAGES.get(app_id, []) if p["name"] != name]
    return _job({"ok": True})

@app.get("/api/apps/{app_id}/scan-imports")
async def scan_imports(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    return {"missing": ["httpx", "aiofiles"], "installed_count": len(FAKE_PACKAGES.get(app_id, []))}

# ---------------------------------------------------------------------------
# Tunnel (Cloudflare)
# ---------------------------------------------------------------------------

@app.get("/api/apps/{app_id}/tunnel")
def list_cf_routes(app_id: int, request: Request):
    _check_auth(request)
    a = _app_or_404(app_id)
    routes = []
    if a.get("external_url"):
        hostname = a["external_url"].replace("https://", "")
        routes.append({
            "hostname": hostname,
            "local_port": a.get("port") or 8000,
            "published": True,
            "cf_route_id": hostname,
        })
    return {"routes": routes}

class CfPublishReq(BaseModel):
    hostname: str
    local_port: int

@app.get("/api/apps/{app_id}/tunnel/check")
def check_cf_hostname(app_id: int, hostname: str, request: Request, local_port: Optional[int] = None):
    _check_auth(request)
    _app_or_404(app_id)
    owner = next((a for a in FAKE_APPS.values()
                  if a["id"] != app_id and a.get("external_url") == f"https://{hostname.strip().lower()}"), None)
    if owner:
        return {"conflict": True, "conflict_type": "local", "owned_by": owner["name"],
                "reason": f"This hostname is already published by '{owner['name']}'. Publishing here will remove it from that app."}
    return {"conflict": False}

@app.post("/api/apps/{app_id}/tunnel/publish")
async def publish_cf_route(app_id: int, req: CfPublishReq, request: Request):
    _check_auth(request)
    a = _app_or_404(app_id)
    for other in FAKE_APPS.values():   # a hostname belongs to one app
        if other.get("external_url") == f"https://{req.hostname}":
            other["external_url"] = None
    a["external_url"] = f"https://{req.hostname}"
    return {"ok": True, "hostname": req.hostname, "url": a["external_url"], "dns_record_id": "fake-dns-id"}

@app.delete("/api/apps/{app_id}/tunnel/publish")
async def unpublish_cf_route(app_id: int, local_port: int, request: Request):
    _check_auth(request)
    a = _app_or_404(app_id)
    a["external_url"] = None
    return {"ok": True}

# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------

@app.get("/api/system/info")
async def system_info(request: Request):
    _check_auth(request)
    return {"ipv4": "192.168.1.100"}

@app.get("/api/system/metrics")
def get_system_metrics(request: Request):
    _check_auth(request)
    return {
        "cpu": {
            "percent": round(random.uniform(5.0, 35.0), 1),
            "load": [round(random.uniform(0.1, 2.0), 2) for _ in range(3)],
            "cores": 8,
        },
        "memory": {
            "total": 16 * 1024**3,
            "available": random.randint(4 * 1024**3, 10 * 1024**3),
            "used": random.randint(4 * 1024**3, 10 * 1024**3),
            "percent": round(random.uniform(40.0, 70.0), 1),
        },
        "disk": {
            "total": 500 * 1024**3,
            "used": 120 * 1024**3,
            "free": 380 * 1024**3,
            "percent": 24.0,
        },
        "local_ip": "192.168.1.100",
        "uptime": random.randint(86400, 864000),
    }

@app.get("/api/system/ports")
async def system_ports(request: Request):
    _check_auth(request)
    listening = [
        {"port": 54783, "pid": os.getpid(), "app_name": None},
        {"port": 8000, "pid": 12345, "app_name": "FastAPI Demo"},
        {"port": 9000, "pid": 55432, "app_name": "Rust Service"},
        {"port": 5432, "pid": 4321, "app_name": None},
    ]
    free = [3000, 3001, 4000, 5000, 8001, 8080, 8081, 8888, 9001, 9090, 9091, 9092]
    return {"listening": listening, "free": free}

# In-memory fake backups — seeded with one legacy (db-only) and one full
# backup so the list, and the "legacy" vs restorable UI paths, aren't empty
# on first load.
FAKE_BACKUPS: List[dict] = [
    {
        "filename": "data-20260901-090000.db",
        "size_bytes": 2_400_000,
        "created_at": (datetime.datetime.utcnow() - datetime.timedelta(days=5)).isoformat() + "Z",
        "type": "database",
    },
    {
        "filename": "full-20260905-120000.tar.gz",
        "size_bytes": 84_000_000,
        "created_at": (datetime.datetime.utcnow() - datetime.timedelta(days=2)).isoformat() + "Z",
        "type": "full",
    },
]

# A backup or restore here is only a clock: the status endpoint works out
# which step "now" falls into, so the progress UI has something real to show
# (including after a reload — the state is the emulator's, not the tab's).
_FAKE_OP_STEPS = {
    "backup": [("Measuring apps and database", 1.5), ("Snapshotting the database", 1.0),
               ("Archiving app files", 9.0), ("Saving the backup", 1.0)],
    "restore": [("Backing up the current state first", 6.0), ("Unpacking and checking the backup", 5.0),
                ("Stopping running apps", 2.0), ("Replacing apps, database and key", 2.0),
                ("Restarting Venvy", 4.0)],
}
FAKE_OP: Optional[dict] = None

def _fake_op_begin(kind: str, filename: Optional[str] = None) -> None:
    global FAKE_OP
    if _fake_op_status() and FAKE_OP["status"] == "running":
        raise HTTPException(409, f"A {FAKE_OP['kind']} is already running — wait for it to finish.")
    FAKE_OP = {
        "id": secrets.token_hex(8), "kind": kind, "status": "running", "step": 0, "detail": "",
        "percent": None, "started": datetime.datetime.utcnow(), "finished_at": None,
        "started_by": "admin", "filename": filename, "size_bytes": None, "error": None,
    }

def _fake_op_status() -> Optional[dict]:
    op = FAKE_OP
    if not op:
        return None
    steps = _FAKE_OP_STEPS[op["kind"]]
    elapsed = (datetime.datetime.utcnow() - op["started"]).total_seconds()
    if op["status"] == "running":
        t = elapsed
        for i, (_label, secs) in enumerate(steps):
            if t < secs:
                measured = op["kind"] == "backup" and i == 2 or op["kind"] == "restore" and i in (0, 1)
                total = 84_000_000
                op.update(step=i, percent=int(t / secs * 100) if measured else None,
                          detail=f"FastAPI Demo — {t / secs * total / 1048576:.1f} MB of {total / 1048576:.1f} MB" if measured else "")
                break
            t -= secs
        else:
            op.update(status="done", step=len(steps) - 1, detail="", percent=None,
                      finished_at=(op["started"] + datetime.timedelta(seconds=sum(d for _, d in steps))).isoformat() + "Z")
            if op["kind"] == "backup":
                ts = datetime.datetime.utcnow()
                op["filename"] = f"full-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"
                op["size_bytes"] = random.randint(40_000_000, 120_000_000)
                FAKE_BACKUPS.insert(0, {"filename": op["filename"], "size_bytes": op["size_bytes"],
                                        "created_at": ts.isoformat() + "Z", "type": "full"})
    out = {k: v for k, v in op.items() if k != "started"}
    out["started_at"] = op["started"].isoformat() + "Z"
    out["steps"] = [label for label, _ in steps]
    out["elapsed_sec"] = int(min(elapsed, sum(d for _, d in steps)))
    return out

@app.post("/api/system/backup")
def create_backup(request: Request):
    _check_auth(request)
    _fake_op_begin("backup")
    return _job({"ok": True, "path": "backups/full-emulated.tar.gz", "size_bytes": 84_000_000})

@app.get("/api/system/backup/status")
def backup_status(request: Request):
    _check_auth(request)
    return {"operation": _fake_op_status()}

@app.post("/api/system/backups/upload/chunk")
async def upload_backup_chunk(
    request: Request,
    upload_id: str = Form(...),
    filename: str = Form(""),
    offset: int = Form(...),
    total: int = Form(...),
    chunk: UploadFile = File(...),
):
    _check_auth(request)
    received = offset + len(await chunk.read())
    if received < total:
        return {"ok": True, "received": received, "done": False}
    ts = datetime.datetime.utcnow()
    name = f"full-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    FAKE_BACKUPS.insert(0, {"filename": name, "size_bytes": total, "created_at": ts.isoformat() + "Z", "type": "full"})
    return {"ok": True, "received": received, "done": True, "filename": name, "size_bytes": total}

@app.delete("/api/system/backups/upload/{upload_id}")
def cancel_backup_upload(upload_id: str, request: Request):
    _check_auth(request)
    return {"ok": True}

@app.get("/api/system/backups")
def list_backups(request: Request):
    _check_auth(request)
    return sorted(FAKE_BACKUPS, key=lambda b: b["created_at"], reverse=True)

@app.get("/api/system/backups/{filename}/download")
def download_backup(filename: str, request: Request):
    _check_auth(request)
    if not any(b["filename"] == filename for b in FAKE_BACKUPS):
        raise HTTPException(404, "Backup not found.")
    return PlainTextResponse(
        f"-- fake backup: {filename} --\n(emulator placeholder, not a real archive)\n",
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.delete("/api/system/backups/{filename}")
def delete_backup(filename: str, request: Request):
    _check_auth(request)
    before = len(FAKE_BACKUPS)
    FAKE_BACKUPS[:] = [b for b in FAKE_BACKUPS if b["filename"] != filename]
    if len(FAKE_BACKUPS) == before:
        raise HTTPException(404, "Backup not found.")
    return {"ok": True}

class RestoreBackupReq(BaseModel):
    confirm: str

@app.post("/api/system/backup/restore/{filename}")
def restore_backup(filename: str, req: RestoreBackupReq, request: Request):
    _check_auth(request)
    if req.confirm != "RESTORE":
        raise HTTPException(400, 'Confirmation phrase did not match — send {"confirm": "RESTORE"}.')
    entry = next((b for b in FAKE_BACKUPS if b["filename"] == filename), None)
    if not entry:
        raise HTTPException(404, "Backup not found.")
    if entry.get("type") != "full":
        raise HTTPException(400, "Only full backups can be restored.")
    # The emulator has no real files/process to restart — the fake operation
    # plays the steps out and then reports "done", which is what the real
    # server says once it is back up, so the UI's whole restore flow runs.
    _fake_op_begin("restore", filename)
    return _job({"ok": True, "restarting": True})

@app.get("/api/system/settings/{key}")
def get_system_setting(key: str, request: Request):
    _check_auth(request)
    if key == "cf_api_token":
        return {"key": key, "value": "", "is_set": bool(FAKE_SETTINGS.get(key))}
    return {"key": key, "value": FAKE_SETTINGS.get(key, "")}

class SystemSettingReq(BaseModel):
    value: str

@app.put("/api/system/settings/{key}")
def set_system_setting(key: str, req: SystemSettingReq, request: Request):
    _check_auth(request)
    FAKE_SETTINGS[key] = req.value
    return {"ok": True, "stored_length": len(req.value)}

class BatchSettingItem(BaseModel):
    key: str
    value: str

@app.post("/api/system/settings/batch")
def set_system_settings_batch(items: List[BatchSettingItem], request: Request):
    _check_auth(request)
    for item in items:
        FAKE_SETTINGS[item.key] = item.value
    return {"ok": True, "saved": len(items)}

@app.get("/api/system/cloudflare/status")
def cf_status(request: Request):
    _check_auth(request)
    token, account, tunnel = (FAKE_SETTINGS.get(k, "") for k in ("cf_api_token", "cf_account_id", "cf_tunnel_id"))
    return {
        "configured": bool(token and account and tunnel),
        "token_set": bool(token),
        "account_id_hint": account[:8],
        "tunnel_id_hint": tunnel[:8],
    }

@app.post("/api/system/cloudflare/test")
async def test_cf_credentials(request: Request):
    _check_auth(request)
    return {
        "ok": True,
        "checks": {
            "account": {"ok": True, "name": "My Cloudflare Account"},
            "tunnel":  {
                "ok": True,
                "name": "venvy-tunnel",
                "id": "fake-tunnel-id-0000",
                "status": "healthy",
                "active_connections": 1,
                "message": "venvy-tunnel (healthy) — 1 active connection(s)",
            },
            "dns_zones": {"ok": True, "message": "Found 2 zone(s) — DNS CNAME records will be managed automatically."},
        },
    }

@app.get("/api/system/cloudflare/zones")
async def list_cf_zones(request: Request):
    _check_auth(request)
    return {
        "ok": True,
        "zones": [
            {"name": "example.com", "id": "zone-id-001"},
            {"name": "myapp.dev", "id": "zone-id-002"},
        ],
    }

# ---------------------------------------------------------------------------
# WebSocket terminal — send fake shell output
# ---------------------------------------------------------------------------

from fastapi import WebSocket

@app.websocket("/api/apps/{app_id}/terminal")
async def terminal_ws(websocket: WebSocket, app_id: int):
    import asyncio
    await websocket.accept()
    # Send a fake shell prompt
    await websocket.send_text("\r\n\033[1;32mvenvy@emulator\033[0m:\033[1;34m~/apps/" + str(app_id) + "\033[0m$ ")
    try:
        while True:
            data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            if data.strip():
                echo = f"\r\n{data.strip()}: command emulated — this is the Venvy UI emulator\r\n\033[1;32mvenvy@emulator\033[0m:\033[1;34m~/apps/{app_id}\033[0m$ "
                await websocket.send_text(echo)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Banner + run
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Deploy, jobs, activity, notifications, alerts, schedule, tokens, shared variables, history
# (static, in-memory: enough to build and look at the UI)
# ---------------------------------------------------------------------------

def _iso(minutes_ago: float = 0) -> str:
    return (datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes_ago)).replace(tzinfo=datetime.timezone.utc).isoformat()

FAKE_GIT: Dict[int, dict] = {}
FAKE_RELEASES: Dict[int, list] = {}
FAKE_DEPLOY_STATE: Dict[int, dict] = {}
FAKE_JOBS_CRON: Dict[int, list] = {}
FAKE_NEXT_ID = {"job": 1, "release": 10, "channel": 1, "alert": 1, "token": 1}
FAKE_CHANNELS: list = [{"id": 1, "name": "Team chat", "kind": "slack", "events": "*", "enabled": True,
                        "config": {"url": "***"}, "last_status": "ok", "last_sent_at": None}]
FAKE_ALERTS: list = [{"id": 1, "app_id": 0, "app": None, "metric": "cpu", "threshold": 90.0, "minutes": 5, "enabled": True,
                      "firing": False, "since": None, "description": "The host: CPU above 90% for 5 min"}]
FAKE_TOKENS: list = []
FAKE_SHARED_ENV: Dict[str, str] = {"TZ": "Europe/Berlin"}
FAKE_SCHEDULE: dict = {"schedule": "0 3 * * *", "keep": 7, "last": {"at": _iso(600), "ok": True, "file": "full-20240601-030000.tar.gz", "size": 4_200_000, "remote": "ok: s3://bk/venvy/full-20240601-030000.tar.gz", "error": None}, "remote": {"kind": "s3", "bucket": "bk", "access_key": "AKID", "secret_key": "***", "region": "eu-west-1"}}
NOTIFY_EVENTS = {
    "app_crashed": "An app died unexpectedly", "app_crash_loop": "An app keeps crashing (restarts back off)",
    "app_unhealthy": "An app's health check keeps failing", "app_recovered": "An unhealthy app is healthy again",
    "deploy_ok": "A deploy succeeded", "deploy_failed": "A deploy failed",
    "deploy_rolled_back": "A deploy failed its health check and was rolled back", "backup_ok": "A scheduled backup finished",
    "backup_failed": "A backup failed", "job_failed": "A scheduled job failed", "alert_fired": "A resource alert fired",
    "alert_cleared": "A resource alert cleared",
}

def _git(app_id: int) -> dict:
    return FAKE_GIT.setdefault(app_id, {"url": "", "branch": "main", "has_token": False, "build_command": "", "auto_deploy": False,
                                        "public_key": None, "ssh": False, "webhook_path": f"/api/hooks/deploy/{app_id}",
                                        "has_webhook_secret": False, "webhook_secret": None})

@app.get("/api/apps/{app_id}/deploy")
def get_deploy(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    st = FAKE_DEPLOY_STATE.get(app_id) or {"running": False, "status": None, "log": [], "started": None, "finished": None, "trigger": None}
    rel = FAKE_RELEASES.setdefault(app_id, [{"id": 1, "created_at": _iso(180), "kind": "deploy", "commit": "9f3c2ab41d", "message": "Fix login redirect", "size": 0, "by": "admin", "restorable": True},
                                            {"id": 2, "created_at": _iso(60 * 30), "kind": "deploy", "commit": "c81e0a77b2", "message": "Add health endpoint", "size": 0, "by": "webhook", "restorable": True}])
    return {"git": _git(app_id), "state": st, "last": {"at": rel[0]["created_at"], "status": "ok", "commit": rel[0]["commit"]}, "releases": rel}

class _GitReq(BaseModel):
    git_url: Optional[str] = None
    git_branch: Optional[str] = None
    build_command: Optional[str] = None
    auto_deploy: Optional[bool] = None
    token: Optional[str] = None

@app.put("/api/apps/{app_id}/git")
def put_git(app_id: int, req: _GitReq, request: Request):
    _check_auth(request); _app_or_404(app_id)
    g = _git(app_id)
    if req.git_url is not None:
        g["url"] = req.git_url; g["ssh"] = req.git_url.startswith("git@") or req.git_url.startswith("ssh://")
    if req.git_branch is not None: g["branch"] = req.git_branch
    if req.build_command is not None: g["build_command"] = req.build_command
    if req.token is not None: g["has_token"] = bool(req.token)
    if req.auto_deploy is not None:
        g["auto_deploy"] = req.auto_deploy
        if req.auto_deploy and not g["webhook_secret"]:
            g["webhook_secret"] = secrets.token_urlsafe(24); g["has_webhook_secret"] = True
    return g

@app.post("/api/apps/{app_id}/git/key")
def make_key(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEmulatedKeyForTheUiOnlyNotARealKey000000 venvy-app-%d" % app_id
    _git(app_id)["public_key"] = key
    return {"public_key": key}

@app.post("/api/apps/{app_id}/git/webhook-secret")
def new_hook_secret(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    g = _git(app_id); g["webhook_secret"] = secrets.token_urlsafe(24); g["has_webhook_secret"] = True
    return {"webhook_secret": g["webhook_secret"]}

class _DeployReq(BaseModel):
    start: bool = False

@app.post("/api/apps/{app_id}/deploy")
async def start_deploy(app_id: int, request: Request, req: _DeployReq = _DeployReq()):
    _check_auth(request); _app_or_404(app_id)
    g = _git(app_id)
    if not g["url"] and not g["build_command"]:
        raise HTTPException(400, "Set a git repository or a build command first.")
    FAKE_DEPLOY_STATE[app_id] = {"running": False, "status": "ok", "started": _iso(), "finished": _iso(), "trigger": "manual", "log": [
        "Deploying (manual, by admin)", "$ git fetch --prune --tags origin +refs/heads/main:refs/remotes/origin/main",
        "  From example.com:you/app", "$ git checkout -f -B main refs/remotes/origin/main", "$ pip install -r requirements.txt",
        "  Requirement already satisfied: fastapi", "Now at 7d41b0e3aa — Emulated commit", "Restarting the app…", "Finished: ok"]}
    rel = FAKE_RELEASES.setdefault(app_id, [])
    FAKE_NEXT_ID["release"] += 1
    rel.insert(0, {"id": FAKE_NEXT_ID["release"], "created_at": _iso(), "kind": "deploy", "commit": "7d41b0e3aa", "message": "Emulated commit", "size": 0, "by": "admin", "restorable": True})
    return {"ok": True}

@app.get("/api/apps/{app_id}/releases")
def get_releases(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    return get_deploy(app_id, request)["releases"]

@app.post("/api/apps/{app_id}/releases")
def make_release(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    FAKE_NEXT_ID["release"] += 1
    FAKE_RELEASES.setdefault(app_id, []).insert(0, {"id": FAKE_NEXT_ID["release"], "created_at": _iso(), "kind": "manual", "commit": None, "message": "Snapshot", "size": 1_200_000, "by": "admin", "restorable": True})
    return _job({"ok": True})

@app.post("/api/apps/{app_id}/releases/{release_id}/rollback")
def rollback(app_id: int, release_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    FAKE_DEPLOY_STATE[app_id] = {"running": False, "status": "ok", "started": _iso(), "finished": _iso(), "trigger": "rollback", "log": ["Rolling back (rollback, by admin)", "Finished: ok"]}
    return {"ok": True}

@app.post("/api/apps/{app_id}/duplicate")
def duplicate(app_id: int, request: Request):
    _check_auth(request)
    src = _app_or_404(app_id)
    global NEXT_APP_ID
    new_id = NEXT_APP_ID; NEXT_APP_ID += 1
    FAKE_APPS[new_id] = {**src, "id": new_id, "name": src["name"] + " copy", "slug": src["slug"] + "-copy", "status": "stopped", "pid": None, "port": None}
    return {**_job(FAKE_APPS[new_id]), "app_id": new_id}

class _JobReq(BaseModel):
    name: str
    schedule: str
    command: str
    timeout: int = 600
    enabled: bool = True

def _job_json(j: dict) -> dict:
    return {**j, "next_run": _iso(-60) if j["enabled"] else None, "running": False}

@app.get("/api/apps/{app_id}/jobs")
def list_cron_jobs(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    jobs = FAKE_JOBS_CRON.setdefault(app_id, [{"id": 0, "name": "Nightly cleanup", "schedule": "0 3 * * *", "command": "python cleanup.py --older-than 30", "timeout": 600, "enabled": True,
                                               "last_run_at": _iso(400), "last_status": "ok", "last_exit_code": 0, "last_duration": 4.2, "last_output": "Removed 12 old files\n"}])
    return [_job_json(j) for j in jobs]

@app.post("/api/apps/{app_id}/jobs")
def create_cron_job(app_id: int, req: _JobReq, request: Request):
    _check_auth(request); _app_or_404(app_id)
    FAKE_NEXT_ID["job"] += 1
    j = {"id": FAKE_NEXT_ID["job"], **req.dict(), "last_run_at": None, "last_status": None, "last_exit_code": None, "last_duration": None, "last_output": ""}
    FAKE_JOBS_CRON.setdefault(app_id, []).append(j)
    return _job_json(j)

@app.put("/api/apps/{app_id}/jobs/{job_id}")
def update_cron_job(app_id: int, job_id: int, req: _JobReq, request: Request):
    _check_auth(request)
    for j in FAKE_JOBS_CRON.get(app_id, []):
        if j["id"] == job_id:
            j.update(req.dict()); return _job_json(j)
    raise HTTPException(404)

@app.delete("/api/apps/{app_id}/jobs/{job_id}")
def delete_cron_job(app_id: int, job_id: int, request: Request):
    _check_auth(request)
    FAKE_JOBS_CRON[app_id] = [j for j in FAKE_JOBS_CRON.get(app_id, []) if j["id"] != job_id]
    return {"ok": True}

@app.post("/api/apps/{app_id}/jobs/{job_id}/run")
def run_cron_job(app_id: int, job_id: int, request: Request):
    _check_auth(request)
    for j in FAKE_JOBS_CRON.get(app_id, []):
        if j["id"] == job_id:
            j.update(last_run_at=_iso(), last_status="ok", last_exit_code=0, last_duration=1.3, last_output="Ran at " + _iso() + "\n")
    return {"ok": True}

@app.get("/api/apps/{app_id}/env-history")
def env_history(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    return [{"ts": _iso(30), "user": "admin", "added": ["API_KEY"], "removed": [], "changed": ["DEBUG"]},
            {"ts": _iso(60 * 24), "user": "admin", "added": ["DEBUG", "HOST"], "removed": [], "changed": []}]

def _series(minutes: int, base: float, wobble: float, scale: float = 1.0) -> list:
    n = min(120, max(10, minutes // 2))
    now = datetime.datetime.utcnow()
    return [{"ts": (now - datetime.timedelta(minutes=minutes * (n - i) / n)).replace(tzinfo=datetime.timezone.utc).isoformat(),
             "cpu": round(max(0, base + wobble * random.uniform(-1, 1)), 2), "mem": (base * 1e6 + wobble * 1e6 * random.uniform(-1, 1)) * scale,
             "disk": 48 + i / n * 2} for i in range(n)]

@app.get("/api/apps/{app_id}/metrics/history")
def app_history(app_id: int, request: Request, minutes: int = 60):
    _check_auth(request); _app_or_404(app_id)
    return {"minutes": minutes, "samples": _series(minutes, 12, 8, 6)}

@app.get("/api/system/metrics/history")
def host_history(request: Request, minutes: int = 60):
    _check_auth(request)
    s = _series(minutes, 22, 10)
    for p in s: p["mem"] = 40 + random.uniform(-4, 4)
    return {"minutes": minutes, "samples": s}

@app.get("/api/apps/{app_id}/disk")
def app_disk(app_id: int, request: Request, refresh: bool = False):
    _check_auth(request); a = _app_or_404(app_id)
    return {"bytes": 38_400_000, "limit_mb": a.get("disk_limit_mb")}

@app.get("/api/apps/{app_id}/logs/search")
def search_logs(app_id: int, request: Request, q: str, regex: bool = False, case: bool = False, limit: int = 200, context: int = 0):
    _check_auth(request); _app_or_404(app_id)
    lines = [(i + 1, l) for i, l in enumerate(SAMPLE_LOG.splitlines())]
    hits = [(n, l) for n, l in lines if (q in l if case else q.lower() in l.lower())]
    return {"matches": [{"file": "app.log", "line": n, "text": l, "before": [lines[n - 2][1]] if context and n > 1 else [], "after": []} for n, l in hits[:limit]],
            "truncated": False, "scanned_bytes": 4000, "files": 1}

@app.get("/api/apps/{app_id}/logs/download")
def download_logs(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    return PlainTextResponse(SAMPLE_LOG, headers={"Content-Disposition": 'attachment; filename="app-logs.txt"'})

@app.get("/api/audit")
def audit(request: Request, limit: int = 100, offset: int = 0, user: str = "", action: str = "", q: str = "", since_hours: int = 0):
    _check_auth(request)
    rows = [{"id": i, "ts": _iso(i * 7), "user": "admin", "action": a, "detail": d, "ip": "192.168.1.20"} for i, (a, d) in enumerate([
        ("app_start", "app 'FastAPI Demo' (id=1)"), ("app_deploy", "app 'FastAPI Demo' (id=1): ok"), ("app_env_change", "app 'FastAPI Demo' (id=1)"),
        ("user_create", "created user 'sam' with role 'viewer'"), ("system_full_backup", "path=full-20240601.tar.gz"), ("token_create", "'ci' (full)")] * 4)]
    rows = [r for r in rows if (not user or r["user"] == user) and (not action or r["action"].startswith(action)) and (not q or q in r["detail"])]
    return {"total": len(rows), "entries": rows[offset:offset + limit]}

@app.get("/api/system/shared-env")
def get_shared_env(request: Request):
    _check_auth(request)
    return {"vars": FAKE_SHARED_ENV, "unreadable": False}

class _SharedReq(BaseModel):
    vars: Dict[str, str]

@app.put("/api/system/shared-env")
def put_shared_env(req: _SharedReq, request: Request):
    _check_auth(request)
    FAKE_SHARED_ENV.clear(); FAKE_SHARED_ENV.update(req.vars)
    return {"ok": True, "count": len(req.vars)}

@app.get("/api/system/notify")
def list_channels(request: Request):
    _check_auth(request)
    return {"events": NOTIFY_EVENTS, "kinds": ["webhook", "slack", "discord", "ntfy", "telegram", "email"], "required": {}, "channels": FAKE_CHANNELS}

class _ChannelReq(BaseModel):
    name: str
    kind: str
    config: Dict[str, Any] = {}
    events: str = "*"
    enabled: bool = True

@app.post("/api/system/notify")
def create_channel(req: _ChannelReq, request: Request):
    _check_auth(request)
    FAKE_NEXT_ID["channel"] += 1
    ch = {"id": FAKE_NEXT_ID["channel"], **req.dict(), "last_status": None, "last_sent_at": None}
    FAKE_CHANNELS.append(ch)
    return ch

@app.put("/api/system/notify/{channel_id}")
def update_channel(channel_id: int, req: _ChannelReq, request: Request):
    _check_auth(request)
    for ch in FAKE_CHANNELS:
        if ch["id"] == channel_id:
            ch.update(req.dict()); return ch
    raise HTTPException(404)

@app.delete("/api/system/notify/{channel_id}")
def delete_channel(channel_id: int, request: Request):
    _check_auth(request)
    FAKE_CHANNELS[:] = [c for c in FAKE_CHANNELS if c["id"] != channel_id]
    return {"ok": True}

@app.post("/api/system/notify/{channel_id}/test")
def test_channel(channel_id: int, request: Request):
    _check_auth(request)
    return {"ok": True, "status": "ok"}

class _AlertReq(BaseModel):
    app_id: int = 0
    metric: str
    threshold: float
    minutes: int = 5
    enabled: bool = True

@app.get("/api/system/alerts")
def list_alerts(request: Request):
    _check_auth(request)
    return FAKE_ALERTS

@app.post("/api/system/alerts")
def create_alert(req: _AlertReq, request: Request):
    _check_auth(request)
    FAKE_NEXT_ID["alert"] += 1
    name = FAKE_APPS[req.app_id]["name"] if req.app_id in FAKE_APPS else None
    who = name or "The host"
    r = {"id": FAKE_NEXT_ID["alert"], **req.dict(), "app": name, "firing": False, "since": None,
         "description": f"{who}: {req.metric} above {req.threshold:g} for {req.minutes} min"}
    FAKE_ALERTS.append(r)
    return r

@app.put("/api/system/alerts/{rule_id}")
def update_alert(rule_id: int, req: _AlertReq, request: Request):
    _check_auth(request)
    for r in FAKE_ALERTS:
        if r["id"] == rule_id:
            r.update(req.dict()); return r
    raise HTTPException(404)

@app.delete("/api/system/alerts/{rule_id}")
def delete_alert(rule_id: int, request: Request):
    _check_auth(request)
    FAKE_ALERTS[:] = [r for r in FAKE_ALERTS if r["id"] != rule_id]
    return {"ok": True}

# Updating Venvy: a newer release is "available". An update goes as the real one does:
# running; Venvy away while the installer restarts it; back as the new version while
# update.sh still finishes; then ok.
FAKE_UPDATE = {"current": "v4.4.2", "latest": "v4.5.0", "state": None, "started_at": None, "checked": time.time() - 180}

@app.get("/api/system/update")
def get_update(request: Request, refresh: bool = False):
    _check_auth(request)
    u = FAKE_UPDATE
    if refresh:
        u["checked"] = time.time()
    if u["state"] == "running":
        took = time.time() - u["started_at"]
        if 4 < took < 7:
            raise HTTPException(503, "Venvy is restarting.")
        if took >= 7:
            u["current"] = u["latest"]
        if took > 10:
            u["state"] = "ok"
    log = [] if not u["state"] else ["Fetching v4.5.0 from https://github.com/nikolaus018/venvy/releases/download/v4.5.0",
                                     "[venvy] Installing Venvy (v4.5.0) — data in /var/lib/venvy, service user venvy",
                                     "[venvy] Building the Python environment for v4.5.0..."] + (["[venvy] Venvy v4.5.0 is running."] if u["state"] == "ok" else [])
    checked = datetime.datetime.fromtimestamp(u["checked"], datetime.timezone.utc).isoformat()
    return {"current": u["current"], "latest": u["latest"], "update_available": u["current"] != u["latest"],
            "check_error": None, "checked_at": checked, "supported": True, "unsupported_reason": None, "state": u["state"],
            "started": _iso() if u["state"] else None, "finished": None, "log": log}

@app.get("/api/system/update/notes")
def get_update_notes(request: Request):
    _check_auth(request)
    behind = FAKE_UPDATE["current"] != FAKE_UPDATE["latest"]
    return {"current": FAKE_UPDATE["current"], "latest": FAKE_UPDATE["latest"], "newer": behind, "error": None, "releases": [
        {"version": "v4.5.0", "items": ["Update Venvy from the UI: System shows the running version and an Update button.",
                                        "The update runs as root through a unit the installer sets up."]},
    ] if FAKE_UPDATE["current"] != FAKE_UPDATE["latest"] else [{"version": "v4.5.0", "items": ["Update Venvy from the UI."]}]}

@app.post("/api/system/update")
def post_update(request: Request):
    _check_auth(request)
    if FAKE_UPDATE["state"] == "running":
        raise HTTPException(409, "An update is already under way.")
    FAKE_UPDATE.update(state="running", started_at=time.time())
    return {"ok": True}

@app.get("/api/system/backup/schedule")
def get_backup_schedule(request: Request):
    _check_auth(request)
    return {**FAKE_SCHEDULE, "next_run": _iso(-300)}

class _ScheduleReq(BaseModel):
    schedule: str = ""
    keep: int = 7

@app.put("/api/system/backup/schedule")
def put_backup_schedule(req: _ScheduleReq, request: Request):
    _check_auth(request)
    FAKE_SCHEDULE.update(schedule=req.schedule, keep=req.keep)
    return {**FAKE_SCHEDULE, "next_run": _iso(-300) if req.schedule else None}

@app.post("/api/system/backup/schedule/run")
def run_backup_schedule(request: Request):
    _check_auth(request)
    return {"ok": True}

@app.put("/api/system/backup/remote")
def put_backup_remote(request: Request, cfg: Dict[str, Any]):
    _check_auth(request)
    FAKE_SCHEDULE["remote"] = {k: ("***" if k in ("secret_key", "password", "private_key") and v else v) for k, v in cfg.items()}
    return {"remote": FAKE_SCHEDULE["remote"]}

@app.delete("/api/system/backup/remote")
def delete_backup_remote(request: Request):
    _check_auth(request)
    FAKE_SCHEDULE["remote"] = None
    return {"ok": True}

@app.post("/api/system/backup/remote/test")
def test_backup_remote(request: Request):
    _check_auth(request)
    return {"ok": True, "where": "s3://bk/venvy/venvy-test.txt"}

@app.get("/api/tokens")
def list_tokens(request: Request):
    _check_auth(request)
    return FAKE_TOKENS

class _TokenReq(BaseModel):
    name: str
    scope: str = "full"
    expires_days: Optional[int] = None

@app.post("/api/tokens")
def create_token(req: _TokenReq, request: Request):
    _check_auth(request)
    FAKE_NEXT_ID["token"] += 1
    raw = "venvy_" + secrets.token_urlsafe(32)
    row = {"id": FAKE_NEXT_ID["token"], "name": req.name, "prefix": raw[:12], "scope": req.scope, "created_at": _iso(),
           "expires_at": _iso(-60 * 24 * req.expires_days) if req.expires_days else None, "last_used_at": None}
    FAKE_TOKENS.insert(0, row)
    return {**row, "token": raw}

@app.delete("/api/tokens/{token_id}")
def delete_token(token_id: int, request: Request):
    _check_auth(request)
    FAKE_TOKENS[:] = [t for t in FAKE_TOKENS if t["id"] != token_id]
    return {"ok": True}

class _AccessReq(BaseModel):
    all_apps: bool = True
    app_ids: List[int] = []

@app.put("/api/users/{user_id}/access")
def set_access(user_id: int, req: _AccessReq, request: Request):
    _check_auth(request)
    return {"ok": True}

@app.post("/api/apps/{app_id}/venv/rebuild")
def rebuild_venv(app_id: int, request: Request):
    _check_auth(request); _app_or_404(app_id)
    return _job({"ok": True, "version": None})

def _banner():
    indigo = "\033[38;5;63m"
    gray   = "\033[90m"
    bold   = "\033[1m"
    reset  = "\033[0m"
    print(f"""
{indigo}{bold}  Venvy Emulator — Windows Dev Mode{reset}
  {gray}All API endpoints return fake data.{reset}
  {gray}Serving the UI from web/ next to this script.{reset}

  {indigo}➜{reset}  {bold}Open:{reset}  http://127.0.0.1:{PORT}

  {gray}Login with:  admin / admin{reset}
  {gray}──────────────────────────────────{reset}
""")

if __name__ == "__main__":
    _banner()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
