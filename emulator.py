"""
Venvy Emulator — Windows-compatible fake backend for UI development.

Serves index.html and responds to every API endpoint with realistic fake data.
No database, no processes, no Linux-only deps (no pty/fcntl/psutil required).

Usage:
    pip install fastapi uvicorn
    python emulator.py

Then open http://localhost:54783 in your browser.
Put index.html in the same folder as this script.
"""

import json
import random
import secrets
import time
import datetime
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Request, Response, Cookie, HTTPException, Query, Form, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 54783
HOST = "127.0.0.1"
ASSETS_DIR = Path(__file__).parent.resolve()

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

@app.get("/sw.js")
def serve_service_worker():
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
    return PlainTextResponse("index.html not found. Put it next to emulator.py.", status_code=404)

# ---------------------------------------------------------------------------
# License
# ---------------------------------------------------------------------------

@app.get("/api/license/status")
def license_status():
    return {"valid": True}

@app.post("/api/license/activate")
async def license_activate(request: Request):
    return {"ok": True}

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
    return [{"id": 1, "username": "admin", "is_admin": True}]

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
        "entrypoint": entrypoint or "main.py",
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
        "memory_limit_mb": 512,
        "cpu_limit_percent": 50,
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
    a["updated_at"] = _now_iso()
    return a

@app.delete("/api/apps/{app_id}")
async def delete_app(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    del FAKE_APPS[app_id]
    FAKE_PACKAGES.pop(app_id, None)
    FAKE_FILE_TREE.pop(app_id, None)
    return {"ok": True}

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

@app.delete("/api/apps/{app_id}/packages/{name}")
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

@app.put("/api/apps/{app_id}/tunnel/publish")
async def publish_cf_route(app_id: int, req: CfPublishReq, request: Request):
    _check_auth(request)
    a = _app_or_404(app_id)
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

@app.post("/api/system/backup")
def create_backup(request: Request):
    _check_auth(request)
    ts = datetime.datetime.utcnow()
    filename = f"full-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    if any(b["filename"] == filename for b in FAKE_BACKUPS):
        raise HTTPException(409, "A backup for this exact timestamp already exists — try again.")
    entry = {
        "filename": filename,
        "size_bytes": random.randint(40_000_000, 120_000_000),
        "created_at": ts.isoformat() + "Z",
        "type": "full",
    }
    FAKE_BACKUPS.insert(0, entry)
    return _job({"ok": True, "path": f"backups/{filename}", "size_bytes": entry["size_bytes"]})

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
    # The emulator has no real files/process to restart — just report success
    # so the UI's restore flow (confirm modal, in-progress overlay, then
    # polling until the server answers again) can be exercised end to end.
    return _job({"ok": True, "restarting": True})

@app.get("/api/system/settings/{key}")
def get_system_setting(key: str, request: Request):
    _check_auth(request)
    if key in ("cf_api_token", "gh_token"):
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
# GitHub (fake device flow + per-app git state)
# ---------------------------------------------------------------------------

FAKE_GH_CONNECTED = False
FAKE_GIT_REPOS: Dict[int, dict] = {}  # app_id -> {branch, remote_url, dirty_count, ahead, behind, last_commit}

class GhClientIdReq(BaseModel):
    client_id: str

@app.put("/api/system/github/client-id")
def set_gh_client_id(req: GhClientIdReq, request: Request):
    _check_auth(request)
    FAKE_SETTINGS["gh_client_id"] = req.client_id
    return {"ok": True}

@app.post("/api/system/github/device/start")
def gh_device_start(request: Request):
    _check_auth(request)
    return {
        "flow_id": secrets.token_hex(8),
        "user_code": "WXYZ-1234",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 1,
    }

@app.post("/api/system/github/device/poll")
def gh_device_poll(request: Request):
    global FAKE_GH_CONNECTED
    _check_auth(request)
    FAKE_GH_CONNECTED = True
    return {"status": "complete", "username": "octocat", "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4"}

@app.get("/api/system/github/status")
def gh_status(request: Request):
    _check_auth(request)
    if not FAKE_GH_CONNECTED:
        return {"connected": False, "client_id_set": bool(FAKE_SETTINGS.get("gh_client_id"))}
    return {"connected": True, "client_id_set": True, "username": "octocat", "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4"}

@app.get("/api/system/github/repos")
def gh_list_repos(request: Request):
    _check_auth(request)
    if not FAKE_GH_CONNECTED:
        raise HTTPException(400, "Not signed in to GitHub.")
    return {"repos": [
        {"full_name": "octocat/Hello-World", "clone_url": "https://github.com/octocat/Hello-World.git", "private": False, "default_branch": "master"},
        {"full_name": "octocat/Spoon-Knife", "clone_url": "https://github.com/octocat/Spoon-Knife.git", "private": False, "default_branch": "main"},
        {"full_name": "octocat/private-notes", "clone_url": "https://github.com/octocat/private-notes.git", "private": True, "default_branch": "main"},
    ]}

@app.post("/api/system/github/disconnect")
def gh_disconnect(request: Request):
    global FAKE_GH_CONNECTED
    _check_auth(request)
    FAKE_GH_CONNECTED = False
    return {"ok": True}

def _git_status_for(app_id: int) -> dict:
    repo = FAKE_GIT_REPOS.get(app_id)
    if not repo:
        return {"is_repo": False}
    return {"is_repo": True, **repo}

@app.get("/api/apps/{app_id}/git/status")
def git_status(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    return _git_status_for(app_id)

class GitCloneReq(BaseModel):
    url: str
    branch: Optional[str] = None

@app.post("/api/apps/{app_id}/git/clone")
def git_clone(app_id: int, req: GitCloneReq, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if app_id in FAKE_GIT_REPOS:
        raise HTTPException(400, "This app is already connected to a repository.")
    FAKE_GIT_REPOS[app_id] = {
        "branch": req.branch or "main",
        "remote_url": req.url,
        "dirty_count": 0, "ahead": 0, "behind": 0,
        "last_commit": "a1b2c3d Initial commit (2 minutes ago)",
    }
    return _job(_git_status_for(app_id))

@app.post("/api/apps/{app_id}/git/fetch")
def git_fetch(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if app_id not in FAKE_GIT_REPOS:
        raise HTTPException(400, "Not a git repository.")
    FAKE_GIT_REPOS[app_id]["behind"] = 2
    return _job(_git_status_for(app_id))

@app.post("/api/apps/{app_id}/git/pull")
def git_pull(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if app_id not in FAKE_GIT_REPOS:
        raise HTTPException(400, "Not a git repository.")
    FAKE_GIT_REPOS[app_id]["behind"] = 0
    return _job(_git_status_for(app_id))

class GitPushReq(BaseModel):
    force: bool = False

@app.post("/api/apps/{app_id}/git/push")
def git_push(app_id: int, req: GitPushReq, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if app_id not in FAKE_GIT_REPOS:
        raise HTTPException(400, "Not a git repository.")
    FAKE_GIT_REPOS[app_id]["ahead"] = 0
    return _job(_git_status_for(app_id))

class GitCommitReq(BaseModel):
    message: str

@app.post("/api/apps/{app_id}/git/commit")
def git_commit(app_id: int, req: GitCommitReq, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if app_id not in FAKE_GIT_REPOS:
        raise HTTPException(400, "Not a git repository.")
    repo = FAKE_GIT_REPOS[app_id]
    repo["dirty_count"] = 0
    repo["ahead"] = repo.get("ahead", 0) + 1
    repo["last_commit"] = f"e5f6a7b {req.message[:50]} (just now)"
    return _job(_git_status_for(app_id))

@app.delete("/api/apps/{app_id}/git")
def git_disconnect(app_id: int, request: Request):
    _check_auth(request)
    _app_or_404(app_id)
    if app_id not in FAKE_GIT_REPOS:
        raise HTTPException(400, "Not a git repository.")
    del FAKE_GIT_REPOS[app_id]
    return {"ok": True}

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

def _banner():
    indigo = "\033[38;5;63m"
    gray   = "\033[90m"
    bold   = "\033[1m"
    reset  = "\033[0m"
    print(f"""
{indigo}{bold}  Venvy Emulator — Windows Dev Mode{reset}
  {gray}All API endpoints return fake data.{reset}
  {gray}Put index.html next to this script.{reset}

  {indigo}➜{reset}  {bold}Open:{reset}  http://127.0.0.1:{PORT}

  {gray}Login with:  admin / admin{reset}
  {gray}──────────────────────────────────{reset}
""")

if __name__ == "__main__":
    _banner()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
