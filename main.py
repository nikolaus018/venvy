"""
Venvy backend — dependency check.
If a required package is missing, we print a clear error instead of
silently installing at runtime (which can corrupt a running venv and
blocks the event loop on startup).

-------------------------------------------------------------------------------
Backup & restore procedure
-------------------------------------------------------------------------------
Venvy's persistent state lives in two places, both under the directory Venvy
is launched from (DATA_DIR = the current working directory at startup):

  1. `data.db` — SQLite database (users, sessions, app metadata, settings).
     Do NOT `cp` this file while Venvy is running — it's in WAL mode, so a
     raw copy can miss data still sitting in the `-wal` file. Instead, use
     the admin-only `POST /api/system/backup` endpoint, which performs a
     consistent snapshot via SQLite's `VACUUM INTO` and returns the path to
     the resulting file. That snapshot is a single ordinary file and is
     always safe to copy with `cp`.
  2. `apps/` — every hosted app's code, venv, and logs. A plain recursive
     copy (`cp -a apps/ backup/apps/` or `rsync -a apps/ backup/apps/`) is
     safe to run at any time; nothing in there uses WAL semantics. For a
     perfectly consistent snapshot, stop apps first (or accept that a
     running app's log file may be mid-write, which is harmless).
  3. `.venvy_key` — the local symmetric-encryption key used to decrypt
     Cloudflare credentials and app env vars stored in `data.db`. **This
     file must be backed up alongside `data.db`** — without it, the
     encrypted columns in a restored database are unrecoverable. Treat it
     with the same care as a private key (mode 0600, never logged).

To restore on a new host:
  1. Stop Venvy (if running).
  2. Place the backed-up `data.db` (or the `VACUUM INTO` snapshot, renamed
     to `data.db`) and `.venvy_key` into the new working directory.
  3. Restore `apps/` into the same working directory.
  4. Start Venvy as usual — it will pick up the restored DB and app files.

There is no one-click restore command; the above is a manual but
straightforward procedure suitable for a small self-hosted team.
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

# TOTP — uses only stdlib (hmac, hashlib, struct, time, base64)
# No external dependency required.

def _totp_generate_secret() -> str:
    """Generate a random 20-byte base32 secret for TOTP."""
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("utf-8")

def _totp_hotp(secret_b32: str, counter: int) -> str:
    """Compute a 6-digit HOTP value."""
    try:
        key = base64.b32decode(secret_b32.upper().replace(" ", ""))
    except Exception:
        return ""
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"

def _totp_verify(secret_b32: str, code: str, window: int = 1) -> bool:
    """Return True if *code* matches the current TOTP ± *window* steps."""
    if not secret_b32 or not code:
        return False
    now = int(time.time()) // 30
    for delta in range(-window, window + 1):
        if hmac.compare_digest(_totp_hotp(secret_b32, now + delta), code.strip()):
            return True
    return False

def _totp_uri(secret_b32: str, username: str, issuer: str = "Venvy") -> str:
    """Build the otpauth:// URI for QR code generation."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    params = urllib.parse.urlencode({
        "secret": secret_b32,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": 6,
        "period": 30,
    })
    return f"otpauth://totp/{label}?{params}"

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

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

if getattr(sys, 'frozen', False):
    ASSETS_DIR = Path(sys._MEIPASS)
else:
    ASSETS_DIR = Path(__file__).parent.resolve()

DATA_DIR = Path.cwd()
APPS_BASE_DIR = DATA_DIR / "apps"
BACKUP_DIR = DATA_DIR / "backups"
DB_FILE = DATA_DIR / "data.db"
DATABASE_URL = f"sqlite:///{DB_FILE}"

SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — keeps PWA users signed in

# Default False so plain-HTTP LAN deployments work out of the box.
# Set SESSION_COOKIE_SECURE=1 in env when running behind HTTPS.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

# ---------------------------------------------------------------------------
# Secrets-at-rest encryption
# ---------------------------------------------------------------------------
from cryptography.fernet import Fernet, InvalidToken

_VENVY_KEY_FILE = DATA_DIR / ".venvy_key"
_ENC_PREFIX = "enc:v1:"
_fernet_instance: Optional[Fernet] = None


def _get_fernet() -> Fernet:
    """Return a process-wide Fernet instance, generating the local key on first run.

    The key never leaves this host, is never logged, and is never returned in
    any API response. Back it up alongside data.db (see module docstring).
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    if not _VENVY_KEY_FILE.exists():
        raw_key = base64.urlsafe_b64encode(secrets.token_bytes(32))
        _VENVY_KEY_FILE.write_bytes(raw_key)
        try:
            _VENVY_KEY_FILE.chmod(0o600)
        except Exception:
            pass
    else:
        try:
            _VENVY_KEY_FILE.chmod(0o600)
        except Exception:
            pass
    _fernet_instance = Fernet(_VENVY_KEY_FILE.read_bytes())
    return _fernet_instance


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* for storage. Empty string stays empty (nothing to protect)."""
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def _decrypt_secret(stored: str) -> str:
    """Decrypt a value written by _encrypt_secret. Tolerates legacy plaintext values
    (i.e. rows written before encryption was introduced) by returning them unchanged."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored
    token = stored[len(_ENC_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception):
        logging.error("Failed to decrypt a stored secret — the .venvy_key file may be missing or changed.")
        return ""


# Tracks all PIDs launched in the current Venvy session to ensure 
# reliable cleanup even if DB state is out of sync. {app_id: pid}
_active_app_pids: Dict[int, int] = {}

# Tracks live resource usage for each app_id. {app_id: {"cpu": float, "memory": int}}
_app_metrics: Dict[int, Dict[str, Union[float, int]]] = {}

# Tracks last global host CPU reading to avoid 0.0% on-demand return.
_host_cpu_percent: float = 0.0

# Track strongly-referenced background tasks to avoid garbage collection midway
_running_tasks: Set[asyncio.Task] = set()

# ---------------------------------------------------------------------------
# ★  DEPLOYMENT CONFIGURATION — Edit before distributing to end-users
# ---------------------------------------------------------------------------
# Optional IP allowlist — comma-separated IPs, e.g. "192.168.1.5,10.0.0.1"
IP_ALLOWLIST_RAW = os.getenv("IP_ALLOWLIST", "").strip()
IP_ALLOWLIST: Optional[Set[str]] = (
    {ip.strip() for ip in IP_ALLOWLIST_RAW.split(",") if ip.strip()} or None
)


def _resolve_client_ip(request: Union[Request, HTTPConnection]) -> str:
    """Helper to resolve the real client IP.

    Venvy is designed to be reached through a Cloudflare Tunnel, so we trust
    `CF-Connecting-IP` — a header Cloudflare's edge sets itself and overwrites
    on the way in, which a client cannot forge. We deliberately do NOT trust
    `X-Forwarded-For`: Cloudflare's edge only *appends* to that header rather
    than replacing it, so the first entry can still be whatever a client
    chose to send, which would let someone bypass the IP allowlist and the
    login lockout just by setting that header themselves. If a request has
    no `CF-Connecting-IP` (i.e. it didn't come through the tunnel), fall back
    to the raw TCP connection IP, which cannot be spoofed either.
    """
    ip = (request.headers.get("cf-connecting-ip", "") or "").strip()
    if not ip and request.client:
        ip = (request.client.host or "").strip()
    return ip or "unknown"


def check_ip_allowlist(request: HTTPConnection) -> None:
    """Dependency: reject requests from IPs not on the allowlist."""
    if IP_ALLOWLIST is None:
        return
    ip = _resolve_client_ip(request)
    if ip == "unknown":
        return  # can't determine IP — allow (likely internal/Unix socket)
    if ip not in IP_ALLOWLIST:
        raise HTTPException(status_code=403, detail="Access from this IP is not allowed.")


# ---------------------------------------------------------------------------
# Database models
# ---------------------------------------------------------------------------

def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def audit_log(username: str, action: str, detail: str = "") -> None:
    """Lightweight audit trail — logs who did what. No dedicated table; these are
    plain INFO log lines an operator can grep/ship to their own log aggregator."""
    suffix = f" — {detail}" if detail else ""
    logging.info(f"AUDIT: user={username!r} action={action}{suffix}")

# Tracks exactly when this Venvy instance started up.
_VENVY_BOOT_TIME = utc_now()


VALID_ROLES = ("admin", "operator", "viewer")


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    # "admin"    — manage users, manage system settings, full app CRUD
    # "operator" — full app CRUD, no user/system-setting management
    # "viewer"   — read-only: status, logs, metrics
    role: str = Field(default="operator")
    created_at: datetime.datetime = Field(default_factory=utc_now)
    totp_secret: Optional[str] = Field(default=None)   # None = TOTP disabled
    totp_enabled: bool = Field(default=False)


class UserSession(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now)
    expires_at: datetime.datetime


class App(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)  # "created_by" audit field only — not used for access control
    name: str = Field(index=True)
    slug: str
    runtime: str = Field(default="python")
    created_at: datetime.datetime = Field(default_factory=utc_now)
    updated_at: datetime.datetime = Field(default_factory=utc_now)

    # Runtime state
    status: str = Field(default="stopped")
    pid: Optional[int] = Field(default=None)

    # Config
    venv_path: str
    app_dir: str
    entrypoint: str
    start_on_boot: bool = Field(default=False)
    restart_policy: str = Field(default="never")  # never | on-failure | always

    # History
    last_exit_code: Optional[int] = Field(default=None)
    last_start_time: Optional[datetime.datetime] = Field(default=None)
    last_stop_time: Optional[datetime.datetime] = Field(default=None)
    # Persisted across Venvy restarts so the "always" restart policy does not
    # immediately re-start apps that were manually stopped before Venvy restarted.
    last_manually_stopped_at: Optional[datetime.datetime] = Field(default=None)

    # Network / environment
    port: Optional[int] = Field(default=None)
    manual_ports: str = Field(default="[]")   # JSON list of integers
    detected_ports: str = Field(default="[]")
    external_url: Optional[str] = Field(default=None)
    env_json: str = Field(default="{}")
    healthcheck_path: Optional[str] = Field(default=None)
    url_source_pref: str = Field(default="local")  # "local" or "public"

    # Resource isolation — enforced via systemd-run when available (see start_app_process_logic).
    # Nullable = unlimited; new apps default to a sane cap.
    memory_limit_mb: Optional[int] = Field(default=512)
    cpu_limit_percent: Optional[int] = Field(default=50)
    # Set when the app is launched under a systemd scope, so stop/kill logic knows
    # to use `systemctl stop` instead of (or in addition to) signal-based teardown.
    systemd_scope: Optional[str] = Field(default=None)


class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = Field(default="")


class CfRoute(SQLModel, table=True):
    """Tracks the Cloudflare public hostname route created for each app."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    local_port: Optional[int] = Field(default=None)  # The specific app port mapped
    hostname: str = Field(default="")          # e.g. myapp.example.com
    cf_route_id: Optional[str] = Field(default=None)  # CF ingress rule id (for deletion)


engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False, "timeout": 20}, 
    echo=False
)

from sqlalchemy import event
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_and_tables() -> None:
    """Create all tables. Called once at startup. Schema changes require a fresh DB."""
    SQLModel.metadata.create_all(engine)
    
    # Simple migration logic for existing DBs
    migrations = [
        "ALTER TABLE app ADD COLUMN runtime VARCHAR DEFAULT 'python'",
        "ALTER TABLE app ADD COLUMN url_source_pref VARCHAR DEFAULT 'local'",
        "ALTER TABLE cfroute ADD COLUMN local_port INTEGER",
        "ALTER TABLE app ADD COLUMN manual_ports VARCHAR DEFAULT '[]'",
        "ALTER TABLE app ADD COLUMN memory_limit_mb INTEGER DEFAULT 512",
        "ALTER TABLE app ADD COLUMN cpu_limit_percent INTEGER DEFAULT 50",
        "ALTER TABLE app ADD COLUMN systemd_scope VARCHAR",
    ]
    
    with engine.begin() as conn:
        for sql in migrations:
            try:
                conn.exec_driver_sql(sql)
            except Exception as e:
                # Silently ignore "duplicate column" errors (expected on subsequent runs).
                # Anything else is a genuine, unexpected migration failure an operator
                # would want to know about, so it's still logged at warning level.
                err_msg = str(e).lower()
                if "duplicate column" not in err_msg and "already exists" not in err_msg:
                    logging.warning(f"Migration failed: {sql} -> {e}")

        # The `role` column is added separately so we can tell whether it was *just*
        # added (i.e. this is an upgrade from a pre-role schema) and, if so, backfill
        # it once from the legacy `is_admin` boolean. On a brand-new DB the column
        # already exists (created by create_all() above), so this ALTER is a no-op
        # duplicate-column error and no backfill runs.
        role_col_is_new = False
        try:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN role VARCHAR DEFAULT 'operator'")
            role_col_is_new = True
        except Exception as e:
            err_msg = str(e).lower()
            if "duplicate column" not in err_msg and "already exists" not in err_msg:
                logging.warning(f"Migration failed: add user.role -> {e}")

        if role_col_is_new:
            try:
                cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()]
                if "is_admin" in cols:
                    conn.exec_driver_sql(
                        "UPDATE user SET role = CASE WHEN is_admin = 1 THEN 'admin' ELSE 'operator' END"
                    )
                    logging.info("Migrated existing users from is_admin to the new role-based access model.")
            except Exception as e:
                logging.warning(f"Role backfill from legacy is_admin failed (existing users default to 'operator'): {e}")

    APPS_BASE_DIR.mkdir(exist_ok=True)


def _psutil_net_connections(kind: str = "tcp"):
    """Wrapper for psutil.net_connections that handles the API change in psutil ≥ 6.0."""
    if psutil is None:
        return []
    try:
        return psutil.net_connections(kind=kind)
    except (AttributeError, Exception):
        return []


def _detect_ports_sync(app_pid: int) -> List[int]:
    """Synchronous function to detect all ports a given PID tree is bound to."""
    pids: List[int] = [app_pid]
    if psutil:
        try:
            pids += [c.pid for c in psutil.Process(app_pid).children(recursive=True)]
        except Exception:
            pass
    else:
        try:
            parent_to_children = {}
            for p in Path("/proc").iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    stat = (p / "stat").read_text().split()
                    ppid = int(stat[3])
                    pid = int(p.name)
                    parent_to_children.setdefault(ppid, []).append(pid)
                except Exception:
                    pass
            queue = [app_pid]
            visited = {app_pid}
            while queue:
                current = queue.pop(0)
                for child in parent_to_children.get(current, []):
                    if child not in visited:
                        visited.add(child)
                        pids.append(child)
                        queue.append(child)
        except Exception:
            pass

    detected_ports: Set[int] = set()

    # ── Primary: /proc fd inode matching ─────────────
    for check_pid in pids:
        try:
            fd_dir = Path(f"/proc/{check_pid}/fd")
            socket_inodes: Set[str] = set()
            for fd_path in fd_dir.iterdir():
                try:
                    link = os.readlink(str(fd_path))
                    if link.startswith("socket:["):
                        socket_inodes.add(link[8:-1])
                except OSError:
                    pass
            if not socket_inodes:
                continue
            for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    for line in Path(tcp_file).read_text().splitlines()[1:]:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        if parts[3] == "0A" and parts[9] in socket_inodes:
                            port_num = int(parts[1].split(":")[1], 16)
                            if port_num > 0:
                                detected_ports.add(port_num)
                except Exception:
                    pass
        except (OSError, PermissionError):
            pass

    # ── Secondary: psutil net_connections ─────────────
    if not detected_ports and psutil:
        pid_set = set(pids)
        for conn in _psutil_net_connections(kind="tcp"):
            if (conn.status == "LISTEN"
                    and conn.laddr
                    and conn.pid is not None
                    and conn.pid in pid_set
                    and conn.laddr.port > 0):
                detected_ports.add(conn.laddr.port)

    return sorted(list(detected_ports))


def get_session():
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=0)
    return base64.b64encode(salt + dk).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        raw = base64.b64decode(hashed.encode("utf-8"))
        salt = raw[:16]
        dk = raw[16:]
        if len(dk) == 32:
            check = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 100_000)
        else:
            check = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=0)
        return secrets.compare_digest(dk, check)
    except Exception:
        return False


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise HTTPException(status_code=400, detail="Password must contain both letters and numbers.")


def _make_tz_aware(dt: datetime.datetime) -> datetime.datetime:
    """Return *dt* with UTC tzinfo attached if it is naive."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    db_session = session.get(UserSession, token)
    if not db_session:
        raise HTTPException(status_code=401, detail="Session invalid.")

    now = utc_now()
    if now > _make_tz_aware(db_session.expires_at):
        session.delete(db_session)
        session.commit()
        raise HTTPException(status_code=401, detail="Session expired.")

    # Slide expiry window if less than 7 days remain.
    remaining = (_make_tz_aware(db_session.expires_at) - now).total_seconds()
    if remaining < 60 * 60 * 24 * 7:
        db_session.expires_at = now + datetime.timedelta(seconds=SESSION_TTL_SECONDS)
        session.add(db_session)
        session.commit()

    user = session.get(User, db_session.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    return user


def check_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return user


def require_operator(user: User = Depends(get_current_user)) -> User:
    """Dependency for any endpoint that mutates apps (start/stop/delete/edit files/
    packages/tunnels). Admins and operators may proceed; viewers are read-only."""
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator or admin privileges required.")
    return user


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def sanitize_slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[\s\W-]+", "-", s)
    return s or "app"


def find_free_port(start: int = 8001, end: int = 9000) -> Optional[int]:
    """Return the first TCP port in [start, end) that can be bound to."""
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    return None


def _verify_port_free(port: int) -> bool:
    """Return True if the port is not currently bound — used immediately before launch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.connect_ex(("localhost", port)) != 0


def is_safe_path(root: Path, target: Path) -> bool:
    """Return True only if *target* resolves to a path strictly inside (or equal to) *root*."""
    try:
        root_r = root.resolve()
        target_r = target.resolve()
        return target_r == root_r or root_r in target_r.parents
    except (ValueError, OSError):
        return False


def get_server_ipv4() -> str:
    """Return the LAN IPv4 of this machine (best-effort)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _build_app_env(db_app: App) -> Dict[str, str]:
    """Build the environment dict for a managed app process."""
    env = os.environ.copy()
    try:
        app_env = json.loads(_decrypt_secret(db_app.env_json) or "{}")
        if isinstance(app_env, dict):
            for k, v in app_env.items():
                env[str(k)] = str(v)
    except Exception:
        pass
    
    effective_port = None
    try:
        m_ports = json.loads(db_app.manual_ports or "[]")
        if m_ports and isinstance(m_ports, list):
            effective_port = m_ports[0]
    except Exception:
        pass
    
    if not effective_port:
        effective_port = db_app.port

    if effective_port:
        env["APP_PORT"] = str(effective_port)
        env["PORT"] = str(effective_port)
    env["PYTHONUNBUFFERED"] = "1"
    
    venv_bin = str(Path(db_app.venv_path) / "bin")
    cargo_bin = str(Path.home() / ".cargo" / "bin")
    
    existing_path = env.get("PATH", "")
    new_paths = [venv_bin]
    if cargo_bin not in existing_path:
        new_paths.append(cargo_bin)
    
    env["PATH"] = ":".join(new_paths + [existing_path]) if existing_path else ":".join(new_paths)
    
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["VIRTUAL_ENV"] = db_app.venv_path
    
    for _bad in ("BASH_ENV", "ENV", "PYTHONSTARTUP"):
        env.pop(_bad, None)
    for _key in list(env.keys()):
        if "PYDEVD" in _key or "DEBUGPY" in _key:
            env.pop(_key, None)
    return {k: str(v) for k, v in env.items()}


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

_log_tasks: Dict[int, asyncio.Task] = {}
_supervisor_event: Optional[asyncio.Event] = None


def _trigger_supervisor() -> None:
    """Wake up the supervisor task immediately."""
    if _supervisor_event:
        _supervisor_event.set()


def _cancel_log_task(app_id: int) -> None:
    task = _log_tasks.pop(app_id, None)
    if task and not task.done():
        task.cancel()


async def log_streamer(stream, log_file: Path) -> None:
    """Read lines from *stream* and append timestamped entries to *log_file*."""
    try:
        async with aiofiles.open(log_file, "a") as f:
            while True:
                line = await stream.readline()
                if not line:
                    break
                ts = utc_now().strftime("%Y-%m-%d %H:%M:%S")
                await f.write(f"[{ts}] {line.decode('utf-8', errors='replace')}")
                await f.flush()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logging.error(f"Log streamer error: {exc}")


def _pid_alive(pid: int) -> bool:
    """Return True if *pid* exists and is not a zombie."""
    if psutil:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # EPERM: process exists, owned by root — alive
    except ProcessLookupError:
        return False  # ESRCH: no such process
    except OSError:
        return False


async def _pid_alive_async(pid: int) -> bool:
    """Non-blocking wrapper — runs _pid_alive in a thread executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _pid_alive, pid)


_SYSTEMD_AVAILABLE: Optional[bool] = None


def _systemd_run_available() -> bool:
    """Best-effort check that `systemd-run`/`systemctl` exist and systemd is PID 1's
    init (i.e. we're actually on a systemd Linux host). Cached after first check."""
    global _SYSTEMD_AVAILABLE
    if _SYSTEMD_AVAILABLE is None:
        _SYSTEMD_AVAILABLE = bool(
            shutil.which("systemd-run")
            and shutil.which("systemctl")
            and Path("/run/systemd/system").exists()
        )
        if not _SYSTEMD_AVAILABLE:
            logging.warning(
                "systemd-run is not available on this host — apps will launch without "
                "per-app CPU/memory limits (expected on non-Linux hosts or containers "
                "without systemd; fine for local dev/testing)."
            )
    return _SYSTEMD_AVAILABLE


async def start_app_process_logic(app_id: int, session: Session) -> App:
    db_app = session.get(App, app_id)
    if not db_app:
        raise ValueError("App not found.")

    if db_app.pid and await _pid_alive_async(db_app.pid):
        db_app.status = "running"
        session.add(db_app)
        session.commit()
        logging.info(f"start_app: '{db_app.name}' already running (PID {db_app.pid})")
        return db_app

    db_app.pid = None

    if not db_app.entrypoint and db_app.runtime != "rust":
        db_app.status = "error"
        session.add(db_app)
        session.commit()
        raise RuntimeError("No entrypoint configured. Set one in the app's Settings tab first.")

    if db_app.runtime == "python":
        venv_python = Path(db_app.venv_path) / "bin" / "python"
        if not venv_python.exists():
            db_app.status = "error"
            session.add(db_app)
            session.commit()
            raise RuntimeError("Virtual environment python not found.")

    app_dir = Path(db_app.app_dir)
    logs_dir = app_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "app.log"

    if log_file.exists() and log_file.stat().st_size > 5 * 1024 * 1024:
        try:
            target = logs_dir / f"app.log.{int(time.time())}"
            await asyncio.to_thread(shutil.move, str(log_file), str(target))
            rotated = sorted(logs_dir.glob("app.log.*"), key=lambda x: x.stat().st_mtime, reverse=True)
            for old_log in rotated[5:]:
                try:
                    await asyncio.to_thread(old_log.unlink)
                except Exception:
                    pass
        except Exception as exc:
            logging.warning(f"Log rotation failed: {exc}")

    if not await asyncio.to_thread(log_file.exists):
        await asyncio.to_thread(log_file.touch)

    env = _build_app_env(db_app)
    _cancel_log_task(app_id)

    try:
        if db_app.runtime == "python":
            venv_python = Path(db_app.venv_path) / "bin" / "python"
            cmd = [str(venv_python), db_app.entrypoint]
        elif db_app.runtime == "rust":
            cmd = ["cargo", "run"]
        else:
            cmd = [db_app.entrypoint]

        stdin_data = None
        effective_port = db_app.port
        if db_app.port:
            port_free = await asyncio.to_thread(_verify_port_free, db_app.port)
            if not port_free:
                new_port = await asyncio.to_thread(find_free_port)
                if new_port:
                    logging.warning(
                        f"Port {db_app.port} for '{db_app.name}' was taken at launch time; "
                        f"reassigning to {new_port}."
                    )
                    db_app.port = new_port
                    try:
                        m_ports = json.loads(db_app.manual_ports or "[]")
                        if m_ports and m_ports[0] == effective_port:
                            m_ports[0] = new_port
                            db_app.manual_ports = json.dumps(m_ports)
                    except Exception:
                        pass
                    env["APP_PORT"] = str(new_port)
                    env["PORT"] = str(new_port)

        unit_name = f"venvy-app-{app_id}"
        launch_cmd = cmd
        used_systemd_scope = False
        if await asyncio.to_thread(_systemd_run_available):
            scope_cmd = ["systemd-run", "--scope", "--quiet", f"--unit={unit_name}"]
            if db_app.memory_limit_mb:
                scope_cmd += ["-p", f"MemoryMax={db_app.memory_limit_mb}M"]
            if db_app.cpu_limit_percent:
                scope_cmd += ["-p", f"CPUQuota={db_app.cpu_limit_percent}%"]
            scope_cmd += ["--"] + cmd
            launch_cmd = scope_cmd
            used_systemd_scope = True

        process = await asyncio.create_subprocess_exec(
            *launch_cmd,
            cwd=str(app_dir),
            stdin=asyncio.subprocess.PIPE if stdin_data else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

        if process.stdout:
            task = asyncio.create_task(log_streamer(process.stdout, log_file))
            _log_tasks[app_id] = task

        db_app.pid = process.pid
        _active_app_pids[app_id] = process.pid
        db_app.status = "running"
        db_app.last_start_time = utc_now()
        db_app.last_exit_code = None
        db_app.systemd_scope = unit_name if used_systemd_scope else None
        session.add(db_app)
        session.commit()
        logging.info(
            f"App '{db_app.name}' started (PID {process.pid})"
            + (f" under systemd scope '{unit_name}.scope'" if used_systemd_scope else " (no resource isolation)")
        )
        
        async def _reap_process(proc: asyncio.subprocess.Process, _app_id: int):
            try:
                rc = await proc.wait()
                logging.info(f"App {_app_id} (PID {proc.pid}) exited with code {rc}")
                try:
                    with Session(engine) as s:
                        app_to_reap = s.get(App, _app_id)
                        if app_to_reap and app_to_reap.pid == proc.pid:
                            app_to_reap.status = "stopped"
                            app_to_reap.pid = None
                            app_to_reap.systemd_scope = None
                            app_to_reap.last_exit_code = rc
                            app_to_reap.last_stop_time = utc_now()
                            s.add(app_to_reap)
                            s.commit()
                except Exception as db_exc:
                    logging.error(f"Failed to update DB for reaped App {_app_id}: {db_exc}")
            except Exception as exc:
                logging.error(f"Error while waiting for App {_app_id}: {exc}")
            finally:
                _active_app_pids.pop(_app_id, None)
                _running_tasks.discard(asyncio.current_task())
                    
            _trigger_supervisor()

        reap_task = asyncio.create_task(_reap_process(process, app_id))
        _running_tasks.add(reap_task)
        
        return db_app
    except Exception as exc:
        logging.error(f"Failed to start app {app_id}: {exc}")
        db_app.status = "error"
        session.add(db_app)
        session.commit()
        raise


async def _kill_pid_tree(pid: int) -> None:
    """Kill a process and its entire descendant tree, async-safely without self-killing."""
    def _get_pids_to_kill(target_pid: int) -> List[int]:
        res = [target_pid]
        if psutil:
            try:
                res += [c.pid for c in psutil.Process(target_pid).children(recursive=True)]
            except Exception:
                pass
        else:
            try:
                for p in Path("/proc").iterdir():
                    if p.name.isdigit():
                        try:
                            stat = (p / "stat").read_text().split()
                            if int(stat[3]) == target_pid:
                                res.append(int(p.name))
                        except Exception:
                            pass
            except Exception:
                pass
        return list(set(res))

    async def _wait_all_dead(pids: List[int], timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not any(_pid_alive(p) for p in pids):
                return True
            await asyncio.sleep(0.05)
        return not any(_pid_alive(p) for p in pids)

    async def _sig_pgid(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except Exception:
            pass

    async def _sig_pid(p: int, sig: int) -> None:
        try:
            os.kill(p, sig)
        except Exception:
            pass

    _SIGTERM_GRACE = 1.0
    _SIGKILL_WAIT  = 0.5

    # Snapshot the full descendant tree up front. A wrapper process (npm, a shell
    # script, a dev-server launcher) can die from SIGTERM before its own child
    # does — if we only waited on the leader pid, we'd declare victory and skip
    # SIGKILL while a descendant is still alive and holding the port.
    pids = await asyncio.to_thread(_get_pids_to_kill, pid)

    try:
        pgid = os.getpgid(pid)
        # CRITICAL BUGFIX: Ensure we never call killpg on Venvy's own PGID
        if pgid > 0 and pgid != os.getpgid(0):
            await _sig_pgid(pgid, signal.SIGTERM)
            if await _wait_all_dead(pids, _SIGTERM_GRACE):
                return
            await _sig_pgid(pgid, signal.SIGKILL)
            await _wait_all_dead(pids, _SIGKILL_WAIT)
            return
    except Exception:
        pass

    # Fallback to individual process teardown if session/PGID check failed
    for p in pids:
        await _sig_pid(p, signal.SIGTERM)

    if await _wait_all_dead(pids, _SIGTERM_GRACE):
        return

    for p in pids:
        if _pid_alive(p):
            await _sig_pid(p, signal.SIGKILL)
    await _wait_all_dead(pids, _SIGKILL_WAIT)


async def stop_app_process_logic(app_id: int, session: Session, manual: bool = False) -> None:
    _cancel_log_task(app_id)

    db_app = session.get(App, app_id)
    if not db_app:
        return
    if not db_app.pid:
        db_app.status = "stopped"
        session.add(db_app)
        session.commit()
        return

    if manual:
        db_app.last_manually_stopped_at = utc_now()

    pid = db_app.pid
    logging.info(f"Stopping app '{db_app.name}' (PID {pid})")

    if db_app.systemd_scope:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "stop", f"{db_app.systemd_scope}.scope",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, sd_stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                logging.debug(
                    f"systemctl stop {db_app.systemd_scope}.scope: "
                    f"{sd_stderr.decode(errors='replace').strip()}"
                )
        except Exception as exc:
            logging.warning(f"systemctl stop failed for {db_app.systemd_scope}.scope: {exc}")

    # Always also run the signal-based teardown as a fallback/complement — the scope
    # unit shares the same PID (systemd-run --scope execs in place), so this is a
    # safe no-op if `systemctl stop` above already tore it down.
    try:
        await _kill_pid_tree(pid)
    except Exception as exc:
        logging.warning(f"Error killing PID {pid}: {exc}")

    db_app.status = "stopped"
    db_app.pid = None
    db_app.systemd_scope = None
    db_app.last_stop_time = utc_now()
    db_app.last_exit_code = -15

    session.add(db_app)
    session.commit()


async def supervisor_task() -> None:
    """Background loop: detect crashed apps, restart per policy, auto-detect ports."""
    global _supervisor_event
    _supervisor_event = asyncio.Event()

    _last_session_cleanup  = 0.0
    _last_login_cleanup    = 0.0

    while True:
        try:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_supervisor_event.wait(), timeout=1.0)
            _supervisor_event.clear()

            now_mono = time.monotonic()

            if now_mono - _last_session_cleanup > 600:
                _last_session_cleanup = now_mono
                try:
                    with Session(engine) as _cs:
                        expired = _cs.exec(
                            select(UserSession).where(UserSession.expires_at < utc_now())
                        ).all()
                        for s in expired:
                            _cs.delete(s)
                        if expired:
                            _cs.commit()
                            logging.info(f"Purged {len(expired)} expired session(s)")
                except Exception as _exc:
                    logging.warning(f"Session cleanup error: {_exc}")

            if now_mono - _last_login_cleanup > 120:
                _last_login_cleanup = now_mono
                expired_keys = [
                    k for k, v in list(_login_attempts.items())
                    if not v.get('locked_until') or time.time() > v['locked_until'] + 300
                ]
                for k in expired_keys:
                    _login_attempts.pop(k, None)

            apps_to_check: List[App] = []
            try:
                with Session(engine) as session:
                    apps_to_check = session.exec(select(App)).all()
            except Exception as e:
                logging.error(f"Failed to fetch apps: {e}")
                await asyncio.sleep(5)
                continue

            for app_row in apps_to_check:
                is_alive = bool(app_row.pid and _pid_alive(app_row.pid))

                if app_row.status == "running" and not is_alive:
                    logging.warning(f"App '{app_row.name}' (PID {app_row.pid}) died unexpectedly.")
                    try:
                        with Session(engine) as update_session:
                            refreshed = update_session.get(App, app_row.id)
                            if refreshed:
                                refreshed.pid = None
                                refreshed.status = "stopped"
                                refreshed.last_stop_time = utc_now()
                                if refreshed.last_exit_code is None:
                                    refreshed.last_exit_code = 1
                                update_session.add(refreshed)
                                update_session.commit()
                    except Exception as db_exc:
                        logging.error(f"Failed to update state for {app_row.name}: {db_exc}")

                    if app_row.restart_policy in ("always", "on-failure"):
                        has_started_this_session = (
                            app_row.last_start_time is not None and
                            _make_tz_aware(app_row.last_start_time) >= _make_tz_aware(_VENVY_BOOT_TIME)
                        )
                        if not has_started_this_session and not app_row.start_on_boot:
                            continue

                        was_manual = (
                            app_row.last_manually_stopped_at is not None
                            and app_row.last_start_time is not None
                            and _make_tz_aware(app_row.last_manually_stopped_at)
                                >= _make_tz_aware(app_row.last_start_time)
                        )
                        if not was_manual:
                            logging.info(f"Restarting '{app_row.name}' (policy: {app_row.restart_policy})")
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(app_row.id, restart_session)
                            except Exception as exc:
                                logging.error(f"Restart failed for '{app_row.name}': {exc}")

                elif app_row.status == "running" and is_alive:
                    try:
                        detected_ports = await asyncio.to_thread(_detect_ports_sync, app_row.pid)
                        manual_list = []
                        try:
                            manual_list = json.loads(app_row.manual_ports or "[]")
                        except Exception:
                            pass
                        
                        if manual_list:
                            combined = manual_list
                        else:
                            combined = [p for p in detected_ports]
                        
                        dp_json = json.dumps(combined)
                        
                        if app_row.detected_ports != dp_json:
                            with Session(engine) as update_session:
                                refreshed = update_session.get(App, app_row.id)
                                if refreshed:
                                    if not refreshed.port and combined:
                                        refreshed.port = combined[0]
                                    refreshed.detected_ports = dp_json
                                    update_session.add(refreshed)
                                    update_session.commit()
                                    logging.info(f"Ports updated to {combined} for '{app_row.name}'")
                    except Exception as exc:
                        logging.debug(f"Port detect scan failed for '{app_row.name}': {exc}")

                elif (app_row.status == "stopped" 
                        and app_row.restart_policy in ("always", "on-failure")
                        and app_row.last_start_time is not None
                        and (app_row.last_stop_time is None or (utc_now() - _make_tz_aware(app_row.last_stop_time)).total_seconds() >= 5.0)):
                    
                    should_restart = False
                    if app_row.restart_policy == "always":
                        should_restart = True
                    elif app_row.restart_policy == "on-failure" and app_row.last_exit_code != 0:
                        should_restart = True
                        
                    if should_restart:
                        has_started_this_session = (
                            app_row.last_start_time is not None and
                            _make_tz_aware(app_row.last_start_time) >= _make_tz_aware(_VENVY_BOOT_TIME)
                        )
                        if not has_started_this_session and not app_row.start_on_boot:
                            continue

                        was_manual = (
                            app_row.last_manually_stopped_at is not None
                            and _make_tz_aware(app_row.last_manually_stopped_at)
                                >= _make_tz_aware(app_row.last_start_time)
                        )
                        if not was_manual:
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(app_row.id, restart_session)
                            except Exception:
                                pass

        except Exception as exc:
            logging.error(f"Supervisor loop error: {exc}")


# ---------------------------------------------------------------------------
# PTY Terminal
# ---------------------------------------------------------------------------

def _write_bashrc(jail_root: Path, venv_bin: Path, app_name_safe: str) -> Path:
    """Write a .bashrc for the app terminal with directory jail enforcement."""
    bashrc_path = jail_root / ".bashrc-venvy"
    activate_script = venv_bin / "activate"
    jail_dir = str(jail_root)

    script = f"""#!/bin/bash
# Activate virtual environment
if [ -f "{activate_script}" ]; then source "{activate_script}"; fi

# ── Venvy Terminal Jail ───────────────────────────────────────────────────────
export JAIL_DIR="{jail_dir}"

# Override cd to block navigation outside the app directory
function cd() {{
    local target jail_resolved
    jail_resolved="$(realpath -m "$JAIL_DIR" 2>/dev/null || echo "$JAIL_DIR")"
    if [ -z "$1" ]; then
        target="$jail_resolved"
    elif [[ "$1" == /* ]]; then
        target="$(realpath -m "$1" 2>/dev/null || echo "$1")"
    else
        target="$(realpath -m "$(builtin pwd)/$1" 2>/dev/null || echo "$(builtin pwd)/$1")"
    fi
    if [[ "$target" != "$jail_resolved" && "$target" != "$jail_resolved/"* ]]; then
        echo -e "\\e[31m✗ Access denied: cannot navigate outside the app directory.\\e[0m"
        return 1
    fi
    builtin cd "$target"
}}

# PROMPT_COMMAND: auto-correct cwd if it ever drifts outside the jail
function _venvy_jail_guard() {{
    local cwd jail_resolved
    cwd="$(builtin pwd -P 2>/dev/null)"
    jail_resolved="$(realpath -m "$JAIL_DIR" 2>/dev/null || echo "$JAIL_DIR")"
    if [[ "$cwd" != "$jail_resolved" && "$cwd" != "$jail_resolved/"* ]]; then
        builtin cd "$JAIL_DIR"
        echo -e "\\e[33m⚠ Returned to app directory (outside access blocked).\\e[0m"
    fi
}}
PROMPT_COMMAND="_venvy_jail_guard"
# ─────────────────────────────────────────────────────────────────────────────

export PS1='\\[\\e[0;32m\\]{app_name_safe}\\[\\e[0m\\] \\[\\e[1;34m\\]\\w\\[\\e[0m\\]\\$ '
alias ls='ls --color=auto'
alias ll='ls -la'
"""
    try:
        bashrc_path.write_text(script, encoding="utf-8")
        bashrc_path.chmod(0o644)
    except Exception as exc:
        logging.error(f"Failed to write .bashrc: {exc}")
        raise
    return bashrc_path


def _collect_metrics_tick(current_app_ids: list, proc_cache: Dict[int, "psutil.Process"]) -> float:  # type: ignore
    """Blocking psutil work for one metrics-collector tick, run via
    asyncio.to_thread so per-app (and per-child-process) /proc reads don't
    stall the event loop — websocket terminal I/O, HTTP responses, and the
    app-supervisor loop all share that loop with this background task.

    proc_cache holds both app-root and child-process Process objects, keyed
    by pid. psutil.Process.cpu_percent(interval=None) always returns 0.0 on
    an instance's first call (it needs a prior sample to diff against) —
    children used to be re-materialized fresh every tick via p.children(),
    so their CPU never accumulated a real sample and always read as 0.0.
    Caching child Process objects across ticks the same way the root
    already was fixes that."""
    host_cpu = psutil.cpu_percent(interval=None)
    live_pids: set = set()

    for app_id in current_app_ids:
        pid = _active_app_pids.get(app_id)
        if not pid:
            continue
        try:
            if pid not in proc_cache:
                p = psutil.Process(pid)
                p.cpu_percent()
                proc_cache[pid] = p

            p = proc_cache[pid]
            live_pids.add(pid)
            with p.oneshot():
                cpu = p.cpu_percent()
                mem = p.memory_info().rss

            for child in p.children(recursive=True):
                cpid = child.pid
                try:
                    if cpid not in proc_cache:
                        child.cpu_percent()  # prime — first read is unreliable, don't use it
                        proc_cache[cpid] = child
                    cached_child = proc_cache[cpid]
                    live_pids.add(cpid)
                    with cached_child.oneshot():
                        cpu += cached_child.cpu_percent()
                        mem += cached_child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    proc_cache.pop(cpid, None)
                    continue

            _app_metrics[app_id] = {"cpu": round(cpu, 1), "memory": mem}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            _app_metrics.pop(app_id, None)
            proc_cache.pop(pid, None)
        except Exception:
            pass

    for pid in list(proc_cache.keys()):
        if pid not in live_pids:
            proc_cache.pop(pid, None)
    return host_cpu


async def metrics_collector_task():
    """Background task to collect CPU/RAM usage for all active app processes."""
    if psutil is None:
        return
    proc_cache: Dict[int, psutil.Process] = {} # type: ignore
    psutil.cpu_percent(interval=None)

    while True:
        try:
            global _host_cpu_percent
            current_app_ids = list(_active_app_pids.keys())
            _host_cpu_percent = await asyncio.to_thread(
                _collect_metrics_tick, current_app_ids, proc_cache
            )
        except Exception as exc:
            logging.error(f"Metrics engine error: {exc}")
        await asyncio.sleep(2.5)


class TerminalManager:

    async def start_terminal(
        self,
        websocket: WebSocket,
        app_id: int,
        session: Session,
        user_id: int,
        user_role: str,
    ) -> None:
        if user_role not in ("admin", "operator"):
            await websocket.close(code=1008, reason="Operator or admin privileges required for terminal access.")
            return
        db_app = session.get(App, app_id)
        if not db_app:
            await websocket.close(code=1008, reason="App not found.")
            return

        jail_root = Path(db_app.app_dir).resolve()
        venv_bin  = Path(db_app.venv_path) / "bin"

        _app_name_safe = re.sub(r"[^A-Za-z0-9_.+\-]", "-", db_app.name)[:24].strip("-") or "app"

        try:
            bashrc_script = _write_bashrc(jail_root, venv_bin, _app_name_safe)
        except Exception as exc:
            logging.error(f"Failed to create bashrc: {exc}")
            await websocket.close()
            return

        _raw_env: Dict[str, str] = {
            "TERM":             "xterm-256color",
            "LANG":             os.environ.get("LANG", "en_US.UTF-8"),
            "HOME":             os.environ.get("HOME", "/tmp"),
            "USER":             os.environ.get("USER", ""),
            "LOGNAME":          os.environ.get("LOGNAME", ""),
            "SHELL":            shutil.which("bash") or "/bin/bash",
            "PATH":             f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "VIRTUAL_ENV":      db_app.venv_path,
            "PYTHONUNBUFFERED": "1",
        }
        env: Dict[str, str] = {k: v for k, v in _raw_env.items() if v}
        try:
            _extra = json.loads(_decrypt_secret(db_app.env_json) or "{}")
            if isinstance(_extra, dict):
                for k, v in _extra.items():
                    env[str(k)] = str(v)
        except Exception:
            pass

        master_fd: int = -1
        slave_fd:  int = -1
        pid:       int = -1

        try:
            master_fd, slave_fd = pty.openpty()

            try:
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 50, 220, 0, 0))
            except OSError:
                pass

            pid = os.fork()

            if pid == 0:
                try:
                    signal.signal(signal.SIGINT,  signal.SIG_DFL)
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

                    os.setsid()
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

                    os.dup2(slave_fd, 0)
                    os.dup2(slave_fd, 1)
                    os.dup2(slave_fd, 2)

                    _keep_fds: Set[int] = set()
                    try:
                        _my_pid = os.getpid()
                        _loopback_inodes: Set[int] = set()
                        for _tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
                            try:
                                with open(_tcp_file) as _tf:
                                    for _line in _tf:
                                        _parts = _line.split()
                                        if len(_parts) < 10:
                                            continue
                                        _local = _parts[1]
                                        _hex_ip = _local.split(":")[0]
                                        if _hex_ip in ("7F000001", "0100007F", "00000001",
                                                       "00000000000000000000000001000000"):
                                            try:
                                                _loopback_inodes.add(int(_parts[9]))
                                            except (ValueError, IndexError):
                                                pass
                            except OSError:
                                pass

                        if _loopback_inodes:
                            _fd_dir = f"/proc/{_my_pid}/fd"
                            for _entry in os.listdir(_fd_dir):
                                try:
                                    _fd_int = int(_entry)
                                    if _fd_int < 3:
                                        continue
                                    _link = os.readlink(f"{_fd_dir}/{_entry}")
                                    if _link.startswith("socket:["):
                                        _inode = int(_link[8:-1])
                                        if _inode in _loopback_inodes:
                                            _keep_fds.add(_fd_int)
                                except (ValueError, OSError):
                                    pass
                    except Exception:
                        pass
                        
                    try:
                        max_fd = os.sysconf("SC_OPEN_MAX")
                    except Exception:
                        max_fd = 4096
                    if not _keep_fds:
                        os.closerange(3, max_fd)
                    else:
                        _prev = 3
                        for _kfd in sorted(_keep_fds):
                            if _kfd > _prev:
                                os.closerange(_prev, _kfd)
                            _prev = _kfd + 1
                        if _prev < max_fd:
                            os.closerange(_prev, max_fd)

                    os.chdir(str(jail_root))
                    bash_exe = env.get("SHELL", "/bin/bash")
                    os.execvpe(
                        bash_exe,
                        [bash_exe, "--rcfile", str(bashrc_script)],
                        env,
                    )
                except Exception:
                    pass
                os._exit(1)

            os.close(slave_fd)
            slave_fd = -1

            loop = asyncio.get_running_loop()
            stop = asyncio.Event()

            async def reader() -> None:
                import codecs
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    while not stop.is_set():
                        try:
                            data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        text = decoder.decode(data)
                        if not text:
                            continue
                        try:
                            await asyncio.wait_for(
                                websocket.send_text(text),
                                timeout=30.0,
                            )
                        except (asyncio.TimeoutError, Exception):
                            break
                finally:
                    stop.set()

            async def writer() -> None:
                try:
                    while not stop.is_set():
                        try:
                            data = await asyncio.wait_for(
                                websocket.receive_text(), timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break
                        if not data:
                            continue
                        if data[0] == '\x00':
                            if len(data) > 4096:
                                continue
                            try:
                                msg = json.loads(data[1:])
                                if "cols" in msg and "rows" in msg:
                                    fcntl.ioctl(
                                        master_fd, termios.TIOCSWINSZ,
                                        struct.pack("HHHH",
                                                    int(msg["rows"]), int(msg["cols"]), 0, 0),
                                    )
                            except Exception:
                                pass
                            continue
                        try:
                            await loop.run_in_executor(None, os.write, master_fd, data.encode("utf-8"))
                        except (OSError, Exception):
                            break
                finally:
                    stop.set()

            t1 = asyncio.create_task(reader())
            t2 = asyncio.create_task(writer())
            try:
                await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.set()
                t1.cancel()
                t2.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)
                
                if pid > 0:
                    try:
                        os.kill(pid, signal.SIGHUP)
                    except OSError:
                        pass
                    async def _reap() -> None:
                        for _ in range(30):
                            try:
                                wpid, _ = os.waitpid(pid, os.WNOHANG)
                                if wpid != 0:
                                    return
                            except ChildProcessError:
                                return
                            await asyncio.sleep(0.1)
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                        try:
                            await loop.run_in_executor(None, os.waitpid, pid, 0)
                        except Exception:
                            pass
                    await _reap()

                if master_fd != -1:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    master_fd = -1

        except Exception as exc:
            logging.error(f"PTY execution error: {exc}")
        finally:
            for fd in (slave_fd, master_fd):
                if fd != -1:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if pid > 0:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                await websocket.close()
            except Exception:
                pass


terminal_manager = TerminalManager()


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

            if app_row.status == "running" and app_row.pid:
                if not _pid_alive(app_row.pid):
                    logging.info(f"Clearing stale status for '{app_row.name}'")
                    app_row.status = "stopped"
                    app_row.pid = None
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
            if app_row.pid and _pid_alive(app_row.pid):
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

_ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS: List[str] = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]

if not _ALLOWED_ORIGINS:
    logging.warning(
        "ALLOWED_ORIGINS is not set. Defaulting to empty origins (Same-origin only). "
        "For external access, set ALLOWED_ORIGINS='https://yourdomain.com' for security."
    )

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
            "https://cdn.tailwindcss.com https://cdnjs.cloudflare.com "
            "https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
            "https://cdn.jsdelivr.net; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss: "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "worker-src blob:; "
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

auth_router = APIRouter(
    prefix="/api/auth", tags=["auth"], dependencies=[Depends(check_ip_allowlist)]
)
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


def _app_safe(app_row: App, reveal_secrets: bool = True) -> dict:
    """Return App as dict — injects local_url + access_url + live metrics.

    env_json is encrypted at rest and holds real secrets (DB passwords, API
    keys, etc. for the hosted app). reveal_secrets=False redacts it instead
    of decrypting, for callers where the requester's role shouldn't see
    plaintext env vars (e.g. a read-only viewer browsing the app list)."""
    d = app_row.model_dump()
    if reveal_secrets:
        d["env_json"] = _decrypt_secret(app_row.env_json) or "{}"
    else:
        d["env_json"] = "{}"
        d["env_redacted"] = bool(app_row.env_json)
    local_url = f"http://{get_server_ipv4()}:{app_row.port}" if app_row.port else None
    d["local_url"] = local_url
    d["metrics"]   = _app_metrics.get(app_row.id, {"cpu": 0.0, "memory": 0})
    d["resource_limits_enforced"] = _systemd_run_available()
    pref = app_row.url_source_pref
    if pref == "public" and app_row.external_url:
        d["access_url"] = app_row.external_url
        d["url_source"] = "cloudflare"
    elif local_url:
        d["access_url"] = local_url
        d["url_source"] = "local"
    else:
        d["access_url"] = None
        d["url_source"] = None
    for key in ("created_at", "updated_at", "last_start_time", "last_stop_time",
                "last_manually_stopped_at"):
        val = d.get(key)
        if isinstance(val, datetime.datetime):
            d[key] = val.isoformat()
    return d


class FileOpReq(BaseModel):
    operation: str
    path: str
    new_path: Optional[str] = None
    content: Optional[str] = None


class PackageReq(BaseModel):
    name: str


class UserReq(BaseModel):
    username: str
    password: str
    role: Optional[str] = None


class PwdReq(BaseModel):
    current_password: str
    password: str
    password_confirm: str


class TotpSetupReq(BaseModel):
    totp_code: str

class TotpDisableReq(BaseModel):
    password: str


# ---------------------------------------------------------------------------
# Auth routes  /  Login rate limiting
# ---------------------------------------------------------------------------
_login_attempts: dict = {}
_login_lock = threading.Lock()
_LOGIN_MAX_TRIES   = 5
_LOGIN_LOCKOUT_SEC = 300

def _login_rate_key(request: Request) -> str:
    return _resolve_client_ip(request)

def _check_login_rate(key: str) -> None:
    with _login_lock:
        entry = _login_attempts.get(key)
        if not entry:
            return
        locked_until = entry.get('locked_until')
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time()) + 1
            raise HTTPException(
                429,
                f'Too many failed attempts. Try again in {remaining} seconds.',
                headers={'X-Retry-After': str(remaining)},
            )

def _record_login_failure(key: str) -> None:
    with _login_lock:
        entry = _login_attempts.setdefault(key, {'count': 0, 'locked_until': None})
        entry['count'] += 1
        if entry['count'] >= _LOGIN_MAX_TRIES:
            entry['locked_until'] = time.time() + _LOGIN_LOCKOUT_SEC
            entry['count'] = 0

def _clear_login_rate(key: str) -> None:
    with _login_lock:
        _login_attempts.pop(key, None)


# ---------------------------------------------------------------------------
# General per-IP rate limiting for app-mutating endpoints
# ---------------------------------------------------------------------------
# Same sliding-window-of-attempts pattern as the login limiter above, but scoped
# to a generous ceiling meant to catch a compromised/buggy frontend session or
# script hammering the host, not to throttle normal interactive use.
_mutation_hits: dict = {}
_mutation_lock = threading.Lock()
_MUTATION_MAX_PER_WINDOW = 60
_MUTATION_WINDOW_SEC = 60


def rate_limit_mutations(request: Request) -> None:
    """Dependency: raise 429 if this IP has made too many mutating requests recently."""
    key = _resolve_client_ip(request)
    now = time.time()
    with _mutation_lock:
        hits = _mutation_hits.setdefault(key, [])
        cutoff = now - _MUTATION_WINDOW_SEC
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= _MUTATION_MAX_PER_WINDOW:
            raise HTTPException(
                429,
                "Too many requests. Please slow down.",
                headers={"X-Retry-After": str(_MUTATION_WINDOW_SEC)},
            )
        hits.append(now)


class UserLoginReq(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


@auth_router.get("/status")
def auth_status(request: Request, session: Session = Depends(get_session)):
    user_exists = session.exec(select(User)).first() is not None
    user = None
    try:
        user = get_current_user(request, session)
    except HTTPException as e:
        if e.status_code not in (401, 403):
            raise
        pass
    return {
        "user_exists": user_exists,
        "authenticated": user is not None,
        "username": user.username if user else None,
        "role": user.role if user else None,
        "totp_enabled": user.totp_enabled if user else False,
    }


@auth_router.post("/setup")
def auth_setup(payload: UserReq, request: Request, response: Response, session: Session = Depends(get_session)):
    if session.exec(select(User)).first():
        raise HTTPException(400, "Setup already completed.")
    _validate_password_strength(payload.password)
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        role="admin",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    token = secrets.token_urlsafe(32)
    session.add(UserSession(
        token=token,
        user_id=user.id,
        expires_at=utc_now() + datetime.timedelta(seconds=SESSION_TTL_SECONDS),
    ))
    session.commit()
    response.set_cookie(
        "session", token, httponly=True, path="/",
        max_age=SESSION_TTL_SECONDS, samesite="lax", secure=SESSION_COOKIE_SECURE,
    )
    audit_log(user.username, "user_setup", "initial admin account created")
    return {"ok": True, "username": user.username, "role": user.role}


@auth_router.post("/login")
def login(
    payload: UserLoginReq,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    rate_key = _login_rate_key(request)
    _check_login_rate(rate_key)

    user = session.exec(select(User).where(User.username == payload.username)).first()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_login_failure(rate_key)
        entry = _login_attempts.get(rate_key, {})
        locked_until = entry.get('locked_until')
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time()) + 1
            raise HTTPException(
                429,
                f'Too many failed attempts. Try again in {remaining} seconds.',
                headers={'X-Retry-After': str(remaining)},
            )
        raise HTTPException(401, "Invalid credentials.")

    if user.totp_enabled and user.totp_secret:
        if not payload.totp_code:
            raise HTTPException(403, "totp_required")
        if not _totp_verify(user.totp_secret, payload.totp_code):
            _record_login_failure(rate_key)
            raise HTTPException(401, "Invalid authenticator code.")

    _clear_login_rate(rate_key)
    token = secrets.token_urlsafe(32)
    session.add(UserSession(
        token=token,
        user_id=user.id,
        expires_at=utc_now() + datetime.timedelta(seconds=SESSION_TTL_SECONDS),
    ))
    session.commit()
    response.set_cookie(
        "session",
        token,
        httponly=True,
        path="/",
        max_age=SESSION_TTL_SECONDS,
        samesite="lax",
        secure=SESSION_COOKIE_SECURE,
    )
    return {"ok": True, "username": user.username, "role": user.role}


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    token = request.cookies.get("session")
    if token:
        db_session = session.get(UserSession, token)
        if db_session:
            session.delete(db_session)
            session.commit()
    response.delete_cookie("session", path="/")
    return {"ok": True}


@auth_router.put("/password")
def change_own_password(
    req: PwdReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not verify_password(req.current_password, user.password_hash):
        raise HTTPException(401, "Current password is incorrect.")
    if req.password != req.password_confirm:
        raise HTTPException(400, "New passwords do not match.")
    _validate_password_strength(req.password)
    user.password_hash = hash_password(req.password)
    session.add(user)
    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# TOTP routes
# ---------------------------------------------------------------------------

@auth_router.get("/totp/setup")
def totp_setup_begin(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Generate (or return pending) TOTP secret + QR URI. Does NOT enable TOTP yet."""
    secret = _totp_generate_secret()
    key = f"totp_pending_{user.id}"
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = secret
    else:
        setting = SystemSetting(key=key, value=secret)
    session.add(setting)
    session.commit()
    uri = _totp_uri(secret, user.username)
    return {"ok": True, "secret": secret, "uri": uri}


@auth_router.post("/totp/setup/confirm")
def totp_setup_confirm(
    req: TotpSetupReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Verify the 6-digit code against the pending secret, then enable TOTP."""
    key = f"totp_pending_{user.id}"
    setting = session.get(SystemSetting, key)
    if not setting or not setting.value:
        raise HTTPException(400, "No pending TOTP setup. Start setup first.")
    if not _totp_verify(setting.value, req.totp_code):
        raise HTTPException(400, "Incorrect code — try again.")
    user.totp_secret = setting.value
    user.totp_enabled = True
    session.add(user)
    session.delete(setting)
    session.commit()
    return {"ok": True}


@auth_router.post("/totp/disable")
def totp_disable(
    req: TotpDisableReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Disable TOTP after verifying current password."""
    if not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "Incorrect password.")
    user.totp_enabled = False
    user.totp_secret = None
    session.add(user)
    session.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# User management (admin only)
# ---------------------------------------------------------------------------

@api_router.get("/users")
def list_users(
    session: Session = Depends(get_session),
    _: User = Depends(check_admin),
):
    return [
        {"id": u.id, "username": u.username, "role": u.role}
        for u in session.exec(select(User)).all()
    ]


@api_router.post("/users")
def create_user(
    req: UserReq,
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
):
    if session.exec(select(User).where(User.username == req.username)).first():
        raise HTTPException(400, "Username taken.")
    _validate_password_strength(req.password)
    role = req.role or "operator"
    if role not in VALID_ROLES:
        raise HTTPException(400, f"role must be one of: {', '.join(VALID_ROLES)}")
    session.add(User(
        username=req.username,
        password_hash=hash_password(req.password),
        role=role,
    ))
    session.commit()
    audit_log(admin.username, "user_create", f"created user '{req.username}' with role '{role}'")
    return {"ok": True}


@api_router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(check_admin),
):
    if user_id == current_user.id:
        raise HTTPException(400, "Cannot delete yourself.")
    user = session.get(User, user_id)
    if user:
        # Apps belong to the instance, not to individual users — deleting a user
        # must never delete or stop any app. Only the account and its sessions go.
        for db_session_row in session.exec(select(UserSession).where(UserSession.user_id == user_id)).all():
            session.delete(db_session_row)
        session.delete(user)
        session.commit()
        audit_log(current_user.username, "user_delete", f"deleted user '{user.username}' (id={user_id})")
    return {"ok": True}


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
            if not _pid_alive(app_row.pid):
                app_row.status = "stopped"
                app_row.pid = None
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
    
    cpu_percent = _host_cpu_percent
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
def create_backup(
    admin: User = Depends(check_admin),
    _rl: None = Depends(rate_limit_mutations),
):
    """Create a consistent point-in-time snapshot of data.db via SQLite's
    VACUUM INTO. Safe to call while Venvy is running (WAL mode) — VACUUM INTO
    reads the database inside its own read transaction, so it never sees a
    half-written state, unlike a raw `cp` of the db file.

    Does not touch apps/ — see the backup & restore notes at the top of this
    file for the full procedure (this endpoint only covers the database).
    """
    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"data-{timestamp}.db"

    # VACUUM INTO writes to a uniquely-named temp file first, then we
    # hard-link it to the final (second-resolution, so collidable) name.
    # os.link() atomically fails if dest already exists — unlike a plain
    # exists()-check-then-write, this means a concurrent request that loses
    # the race can NEVER delete the winner's completed backup, because it
    # only ever unlinks the temp path it created itself, never `dest`.
    tmp_dest = BACKUP_DIR / f".tmp-{timestamp}-{secrets.token_hex(4)}.db"
    try:
        conn = sqlite3.connect(str(DB_FILE))
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_dest),))
        finally:
            conn.close()
    except Exception as e:
        logging.error(f"Database backup failed: {e}")
        tmp_dest.unlink(missing_ok=True)
        raise HTTPException(500, f"Backup failed: {e}")

    try:
        os.link(str(tmp_dest), str(dest))
    except FileExistsError:
        raise HTTPException(409, "A backup for this exact timestamp already exists — try again.")
    finally:
        tmp_dest.unlink(missing_ok=True)

    audit_log(admin.username, "system_backup", f"path={dest}")
    return {"ok": True, "path": str(dest), "size_bytes": dest.stat().st_size}


@api_router.get("/system/backups")
def list_backups(admin: User = Depends(check_admin)):
    """List existing database backups created via POST /api/system/backup."""
    if not BACKUP_DIR.exists():
        return []
    return sorted(
        (
            {"filename": f.name, "size_bytes": f.stat().st_size, "created_at": datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc).isoformat()}
            for f in BACKUP_DIR.glob("data-*.db")
        ),
        key=lambda b: b["created_at"],
        reverse=True,
    )


_BACKUP_FILENAME_RE = re.compile(r"\Adata-\d{8}-\d{6}\.db\Z")


def _resolve_backup_path(filename: str) -> Path:
    """Validate a backup filename against the fixed naming scheme used by
    create_backup(), resolve it inside BACKUP_DIR, and confirm the resolved
    path is actually still inside BACKUP_DIR (matches the is_safe_path
    pattern used for app file access elsewhere in this file) — rejects
    anything that isn't an exact, contained match."""
    if not _BACKUP_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid backup filename.")
    path = BACKUP_DIR / filename
    if not is_safe_path(BACKUP_DIR, path) or not path.is_file():
        raise HTTPException(404, "Backup not found.")
    return path


@api_router.get("/system/backups/{filename}/download")
def download_backup(filename: str, admin: User = Depends(check_admin)):
    """Download a previously created database backup snapshot."""
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
    """Delete a database backup snapshot."""
    path = _resolve_backup_path(filename)
    path.unlink()
    audit_log(admin.username, "system_backup_delete", f"path={path}")
    return {"ok": True}


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
    await asyncio.to_thread((app_dir / "logs").mkdir, mode=0o750)

    _MAX_UPLOAD_BYTES = 50 * 1024 * 1024
    for file in (files or []):
        rel_path = file.filename or "upload"
        if "\0" in rel_path or ".." in rel_path or rel_path.startswith("/"):
            continue
            
        safe_name = Path(rel_path).name
        if not safe_name or safe_name.startswith(".") or safe_name.startswith(".jail"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.+\-\s()[\]@#!~]+", safe_name):
            continue
            
        dest = (app_dir / rel_path).resolve()
        if not is_safe_path(app_dir, dest):
            continue
            
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        
        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(413, f"File '{safe_name}' exceeds 50 MB limit.")
        async with aiofiles.open(dest, "wb") as f:
            await f.write(data)

    venv_path = app_dir / "venv"
    if runtime == "python":
        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", "venv", str(venv_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, venv_err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(500, "venv creation timed out.")
        if proc.returncode != 0:
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            err_msg = venv_err.decode(errors="replace")
            hint = ""
            if "ensurepip" in err_msg or "venv" in err_msg.lower():
                hint = f" Hint: python3-venv may not be installed — run: sudo apt-get install -y python3.{sys.version_info.minor}-venv"
            raise HTTPException(500, f"venv creation failed: {err_msg}{hint}")

    if requirements and requirements.filename:
        if runtime == "python":
            req_path = app_dir / "requirements.txt"
            async with aiofiles.open(req_path, "wb") as f:
                await f.write(await requirements.read())
            pip_bin = venv_path / "bin" / "pip"
            if not await asyncio.to_thread(pip_bin.exists):
                await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
                raise HTTPException(500, "pip not found in the new virtual environment.")
            proc = await asyncio.create_subprocess_exec(
                str(pip_bin), "install", "--no-input", "-r", str(req_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, pip_err = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
                raise HTTPException(500, "pip install timed out after 10 minutes.")
            if proc.returncode != 0:
                await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
                raise HTTPException(500, f"pip install failed: {pip_err.decode()}")

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
    )
    session.add(new_app)
    session.commit()
    session.refresh(new_app)
    audit_log(user.username, "app_create", f"created app '{new_app.name}' (id={new_app.id})")
    return _app_safe(new_app)


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
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_restart():
        try:
            with Session(engine) as s:
                await stop_app_process_logic(app_id, s, manual=False)
            await asyncio.sleep(0.1)
            with Session(engine) as s:
                refreshed = s.get(App, app_id)
                if refreshed and refreshed.pid and _pid_alive(refreshed.pid):
                    refreshed.pid = None
                    refreshed.status = "stopped"
                    s.add(refreshed)
                    s.commit()
            with Session(engine) as s:
                await start_app_process_logic(app_id, s)
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
        name_clean = req.name.strip()
        if not name_clean:
            raise HTTPException(400, "App name cannot be empty.")
        if len(name_clean) > 50:
            raise HTTPException(400, "App name must be 50 characters or less.")
        app_row.name = name_clean
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
        app_row.env_json = _encrypt_secret(json.dumps(req.env_vars))
        audit_log(user.username, "app_env_change", f"app '{app_row.name}' (id={app_id})")
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
    if req.runtime is not None:
        if req.runtime not in ("python", "rust"):
            raise HTTPException(400, "runtime must be one of: python, rust")
        app_row.runtime = req.runtime
        if app_row.runtime == "rust":
            app_row.entrypoint = "main.rs"
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
    app_name = app_row.name
    _cancel_log_task(app_id)
    await stop_app_process_logic(app_id, session)

    cf_route = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).first()
    if cf_route and cf_route.cf_route_id:
        try:
            creds      = _get_cf_creds(session)
            account_id = creds["cf_account_id"]
            tunnel_id  = creds["cf_tunnel_id"]
            api_token  = creds["cf_api_token"]
            cfg_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                f"/cfd_tunnel/{tunnel_id}/configurations"
            )
            hostname = cf_route.hostname
            current_cfg = await _cf_request("GET", cfg_url, api_token)
            result_obj  = current_cfg.get("result") or {}
            ingress     = result_obj.get("config", {}).get("ingress") or []
            named       = [r for r in ingress if r.get("hostname") and r["hostname"] != hostname]
            catch_all   = [r for r in ingress if not r.get("hostname")]
            new_ingress = named + (catch_all or [{"service": "http_status:404"}])
            await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}})
            zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
            if zone_id:
                await _cf_delete_dns_cname(zone_id, hostname, api_token)
            logging.info(f"CF route cleaned up for deleted app {app_id} ({hostname})")
        except Exception as exc:
            logging.warning(f"CF cleanup failed for app {app_id} during delete: {exc}")
    if cf_route:
        session.delete(cf_route)

    app_dir_path = Path(app_row.app_dir)
    if await asyncio.to_thread(app_dir_path.exists):
        await asyncio.to_thread(shutil.rmtree, app_row.app_dir, ignore_errors=True)
    session.delete(app_row)
    session.commit()
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
    if app_row.pid and psutil:
        try:
            p = psutil.Process(app_row.pid)
            stats["cpu"] = p.cpu_percent(interval=None)
            stats["memory"] = p.memory_info().rss / (1024 * 1024)
            if app_row.last_start_time:
                stats["uptime"] = (utc_now() - _make_tz_aware(app_row.last_start_time)).total_seconds()
        except Exception:
            pass
    return stats


@api_router.get("/apps/{app_id}/files/tree")
def file_tree(
    app_id: int,
    path: str = "",
    session: Session = Depends(get_session),
    # A viewer's role is explicitly "read-only: status, logs, metrics" — not
    # file contents. App directories routinely contain .env files, config
    # with DB credentials, service-account JSON, etc., so this needs the same
    # gate as file_operations() (mutations) and env_json redaction, not the
    # bare-authenticated get_current_user any role can satisfy.
    user: User = Depends(require_operator),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target = (root / path).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(403)
    results = []
    _SKIP_NAMES = {"venv", "__pycache__", "logs", "node_modules", ".git", ".mypy_cache", ".pytest_cache"}
    if target.is_dir():
        for item in target.iterdir():
            if item.name.startswith(".") or item.name in _SKIP_NAMES:
                continue
            try:
                size = item.stat().st_size
            except OSError:
                size = 0
            results.append({
                "name": item.name,
                "path": item.relative_to(root).as_posix(),
                "is_dir": item.is_dir(),
                "size": size,
            })
    return sorted(results, key=lambda x: (not x["is_dir"], x["name"].lower()))


@api_router.post("/apps/{app_id}/files/op")
async def file_operations(
    app_id: int,
    req: FileOpReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target = (root / req.path).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(403)
    if target.name.startswith(".jailrc") or target.name.startswith(".jailsh"):
        raise HTTPException(403, "Operation not permitted on this path.")

    try:
        if req.operation == "create_file":
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.touch)

        elif req.operation == "create_folder":
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)

        elif req.operation == "delete":
            if await asyncio.to_thread(target.is_dir):
                await asyncio.to_thread(shutil.rmtree, target, True)
            else:
                await asyncio.to_thread(target.unlink, missing_ok=True)

        elif req.operation == "rename":
            new_target = (root / (req.new_path or "")).resolve()
            if not is_safe_path(root, new_target):
                raise HTTPException(403)
            if new_target.name.startswith(".jailrc") or new_target.name.startswith(".jailsh"):
                raise HTTPException(403, "Operation not permitted on this path.")
            await asyncio.to_thread(new_target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.rename, new_target)

        elif req.operation == "copy":
            new_target = (root / (req.new_path or "")).resolve()
            if not is_safe_path(root, new_target):
                raise HTTPException(403)
            if new_target.name.startswith(".jailrc") or new_target.name.startswith(".jailsh"):
                raise HTTPException(403, "Operation not permitted on this path.")
            if target.is_dir():
                await asyncio.to_thread(shutil.copytree, target, new_target)
            else:
                await asyncio.to_thread(new_target.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(shutil.copy2, target, new_target)

        elif req.operation == "save":
            if req.content is None:
                raise HTTPException(400, "No content provided for save.")
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            async with aiofiles.open(target, "w", encoding="utf-8") as _f:
                await _f.write(req.content)
        else:
            raise HTTPException(400, f"Unknown operation: {req.operation}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"File operation failed: {exc}")
    return {"ok": True}


@api_router.get("/apps/{app_id}/files/download")
async def download_file(
    app_id: int,
    path: str,
    session: Session = Depends(get_session),
    # See file_tree() above — same reasoning, viewers don't get file contents.
    user: User = Depends(require_operator),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target = (root / path).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(403)
    if not target.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(
        target,
        filename=target.name,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
        },
    )


@api_router.post("/apps/{app_id}/files/upload")
async def upload_file_to_path(
    app_id: int,
    path: str = Form(""),
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target_dir = (root / path).resolve()
    if not is_safe_path(root, target_dir):
        raise HTTPException(403)
    if not target_dir.is_dir():
        raise HTTPException(404, "Target directory does not exist.")
    _MAX_UPLOAD_BYTES = 50 * 1024 * 1024
    for file in files:
        rel_path = file.filename or "upload"
        if "\0" in rel_path or ".." in rel_path or rel_path.startswith("/"):
            continue
            
        safe_name = Path(rel_path).name
        if not safe_name or safe_name.startswith(".") or safe_name.startswith(".jail"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.+\-\s()[\]@#!~]+", safe_name) or "\0" in path:
            raise HTTPException(400, f"Filename '{safe_name}' contains invalid characters.")
            
        dest = (target_dir / rel_path).resolve()
        if not is_safe_path(root, dest):
            raise HTTPException(403, "Upload path escapes app directory.")
            
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        
        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File '{safe_name}' exceeds 50 MB limit.")
        async with aiofiles.open(dest, "wb") as f:
            await f.write(data)
    return {"ok": True}


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
        manifest_path = Path(app_row.app_dir) / "Cargo.toml"
        if not manifest_path.exists():
            return []
        if not _toml:
            raise HTTPException(500, "tomllib not available (requires Python 3.11+)")
        try:
            with open(manifest_path, "rb") as f:
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
    
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    
    proc = await asyncio.create_subprocess_exec(
        str(venv_python), "-m", "pip", "list", "--format=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
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


@api_router.post("/apps/{app_id}/packages")
async def install_package(
    app_id: int,
    req: PackageReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
        
    pkg_name = req.name.strip()
    
    if app_row.runtime == "rust":
        # Same allow-list style as the Python path below — `cargo add` has no shell,
        # so this is defense-in-depth rather than a shell-injection fix, but it keeps
        # the validation consistent across runtimes and rejects obviously bogus input.
        if not re.fullmatch(r"[A-Za-z0-9_.+\-]+(@[A-Za-z0-9_.+\-]+)?", pkg_name):
            raise HTTPException(400, "Invalid package name.")
        manifest_path = Path(app_row.app_dir) / "Cargo.toml"
        proc = await asyncio.create_subprocess_exec(
            "cargo", "add", pkg_name, "--manifest-path", str(manifest_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(500, "Cargo add timed out.")
        if proc.returncode != 0:
            raise HTTPException(500, f"Cargo add failed: {stderr.decode()}")
        audit_log(user.username, "package_install", f"app '{app_row.name}' (id={app_id}): {pkg_name}")
        return {"ok": True}

    if app_row.runtime != "python":
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")
        
    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, "Virtual environment python not found.")
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+(\[[\w,]+\])?([\s]*(==|>=|<=|~=|!=|>|<)[^\s]*)?", pkg_name):
        raise HTTPException(400, "Invalid package name.")
        
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    proc = await asyncio.create_subprocess_exec(
        str(venv_python), "-m", "pip", "install", "--no-input", pkg_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, "Install timed out after 5 minutes.")
    if proc.returncode != 0:
        raise HTTPException(500, f"Install failed: {stderr.decode()}")
    audit_log(user.username, "package_install", f"app '{app_row.name}' (id={app_id}): {pkg_name}")
    return {"ok": True}


@api_router.delete("/apps/{app_id}/packages/{name}")
async def uninstall_package(
    app_id: int,
    name: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
        
    if app_row.runtime == "rust":
        if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name):
            raise HTTPException(400, "Invalid package name.")
        manifest_path = Path(app_row.app_dir) / "Cargo.toml"
        proc = await asyncio.create_subprocess_exec(
            "cargo", "remove", name, "--manifest-path", str(manifest_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(502, f"Cargo remove failed: {stderr.decode()}")
        audit_log(user.username, "package_uninstall", f"app '{app_row.name}' (id={app_id}): {name}")
        return {"ok": True}

    if app_row.runtime != "python":
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")
        
    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, "Virtual environment python not found.")
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name):
        raise HTTPException(400, "Invalid package name.")
        
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    proc = await asyncio.create_subprocess_exec(
        str(venv_python), "-m", "pip", "uninstall", "-y", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, "Uninstall timed out.")
    if proc.returncode != 0:
        raise HTTPException(500, f"Uninstall failed: {stderr.decode()}")
    audit_log(user.username, "package_uninstall", f"app '{app_row.name}' (id={app_id}): {name}")
    return {"ok": True}


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
    log_file = Path(app_row.app_dir) / "logs" / "app.log"
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
    log_file = Path(app_row.app_dir) / "logs" / "app.log"
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
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = str(app_row.venv_path)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)

        proc = await asyncio.create_subprocess_exec(
            str(venv_python), "-m", "pip", "list", "--format=json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
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
            text = await asyncio.to_thread(py_file.read_text, encoding="utf-8", errors="replace")
            tree = _ast.parse(text)
        except SyntaxError:
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
    _: User = Depends(get_current_user),
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
    if not setting:
        return {"key": key, "value": ""}
    value = setting.value
    if key.startswith("cf_"):
        value = _decrypt_secret(value)
    return {"key": key, "value": value}


def _sanitize_cf_value(v: str) -> str:
    """Strip ALL whitespace and invisible/zero-width Unicode chars."""
    v = v.strip()
    v = "".join(ch for ch in v if ch not in "\r\n\t")
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")
    return v


@api_router.put("/system/settings/{key}")
def set_system_setting(
    key: str,
    req: SystemSettingReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    value = req.value
    if key.startswith("cf_"):
        value = _sanitize_cf_value(value)
        stored_value = _encrypt_secret(value)
    else:
        stored_value = value
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
        value = item.value
        if item.key.startswith("cf_"):
            value = _sanitize_cf_value(value)
            stored_value = _encrypt_secret(value)
        else:
            stored_value = value
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


# ---------------------------------------------------------------------------
# Cloudflare Tunnel Route Management
# ---------------------------------------------------------------------------

def _get_cf_creds(session: Session) -> dict:
    """Return sanitized dict with api_token, account_id, tunnel_id or raise 400."""
    keys = ("cf_api_token", "cf_account_id", "cf_tunnel_id")
    result = {}
    for k in keys:
        row = session.get(SystemSetting, k)
        raw = row.value if row else ""
        decrypted = _decrypt_secret(raw) if raw else ""
        result[k] = _sanitize_cf_value(decrypted)
    missing = [k for k, v in result.items() if not v]
    if missing:
        raise HTTPException(400, f"Cloudflare not configured. Missing: {', '.join(missing)}")
    tok = result["cf_api_token"]
    logging.info(f"CF creds loaded: token_len={len(tok)}")
    return result


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


# ---------------------------------------------------------------------------
# Cloudflare API helper
# ---------------------------------------------------------------------------

async def _cf_request(method: str, url: str, token: str, payload: Optional[dict] = None) -> dict:
    """Make an asynchronous Cloudflare API call with proper timeout and error handling."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, json=payload) as resp:
                data = await resp.json()
                if not (200 <= resp.status < 300):
                    errs = data.get("errors") or []
                    msg = errs[0].get("message") if errs else f"HTTP {resp.status}"
                    raise HTTPException(resp.status, f"Cloudflare API Error: {msg}")
                return data
    except asyncio.TimeoutError:
        raise HTTPException(504, "Cloudflare API request timed out (10s).")
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Cloudflare connection error: {str(exc)}")
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, f"Internal error during Cloudflare request: {str(exc)}")


class CfPublishReq(BaseModel):
    hostname: str
    local_port: int


async def _cf_find_zone_id(hostname: str, account_id: str, api_token: str) -> Optional[str]:
    """Resolve the Cloudflare Zone ID for the domain that owns *hostname*."""
    parts = hostname.split(".")
    candidates = [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
    if hostname not in candidates:
        candidates.insert(0, hostname)
    candidates = list(dict.fromkeys(candidates))
    for candidate in candidates:
        url = (
            f"https://api.cloudflare.com/client/v4/zones"
            f"?name={candidate}&account.id={account_id}&status=active"
        )
        try:
            data = await _cf_request("GET", url, api_token)
            zones = data.get("result") or []
            if zones:
                logging.info(f"CF zone found: {candidate!r} → {zones[0]['id']}")
                return zones[0]["id"]
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise HTTPException(
                    403,
                    "Cloudflare API token lacks Zone:Read permission. "
                    "Add Zone:Read and DNS:Edit to your token, then retry."
                )
            raise
        except Exception:
            continue
    return None


async def _cf_upsert_dns_cname(
    zone_id: str,
    hostname: str,
    tunnel_id: str,
    api_token: str,
) -> str:
    """Create or update a CNAME DNS record in Cloudflare pointing to the tunnel."""
    target = f"{tunnel_id}.cfargotunnel.com"

    record_body = {
        "type":    "CNAME",
        "name":    hostname,
        "content": target,
        "ttl":     1,
        "proxied": True,
    }

    list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=CNAME&name={hostname}&per_page=100"
    existing = await _cf_request("GET", list_url, api_token)
    cname_records = existing.get("result") or []

    for rec_type in ("A", "AAAA"):
        conflict_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type={rec_type}&name={hostname}&per_page=100"
        try:
            conflict_data = await _cf_request("GET", conflict_url, api_token)
            for rec in conflict_data.get("result") or []:
                logging.warning(f"Deleting conflicting {rec_type} record {rec['id']} for {hostname}")
                await _cf_request(
                    "DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                    api_token,
                )
        except Exception as exc:
            logging.warning(f"Could not clear {rec_type} records for {hostname}: {exc}")

    if cname_records:
        for extra in cname_records[1:]:
            try:
                await _cf_request(
                    "DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{extra['id']}",
                    api_token,
                )
            except Exception as exc:
                logging.warning(f"Could not delete duplicate CNAME {extra['id']}: {exc}")
        record_id = cname_records[0]["id"]
        resp = await _cf_request(
            "PUT",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
            api_token,
            record_body,
        )
    else:
        resp = await _cf_request(
            "POST",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            api_token,
            record_body,
        )

    if not resp.get("success"):
        errs = resp.get("errors") or []
        err_msgs = "; ".join(e.get("message", str(e)) for e in errs)
        raise HTTPException(502, f"DNS CNAME upsert failed: {err_msgs or errs}")

    result = resp.get("result") or {}
    record_id = result.get("id", "unknown")
    logging.info(
        f"DNS CNAME upserted: {hostname} → {target} "
        f"(record {record_id}, proxied={result.get('proxied')})"
    )
    return record_id


async def _cf_delete_dns_cname(
    zone_id: str,
    hostname: str,
    api_token: str,
) -> None:
    """Delete the CNAME DNS record for *hostname* in the given zone, if it exists."""
    list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=CNAME&name={hostname}"
    try:
        existing = await _cf_request("GET", list_url, api_token)
        for rec in existing.get("result", []):
            await _cf_request(
                "DELETE",
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                api_token,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning(f"DNS record deletion failed for {hostname}: {exc}")


@api_router.get("/apps/{app_id}/tunnel/check")
async def check_cf_hostname(
    app_id: int,
    hostname: str = Query(...),
    local_port: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Pre-flight check before publishing a hostname."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    clean = hostname.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not clean:
        raise HTTPException(400, "Invalid hostname.")

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

    new_hostname = req.hostname.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if not new_hostname or "." not in new_hostname:
        raise HTTPException(400, "Invalid hostname — must contain at least one dot (e.g. app.example.com).")

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
    
    if not other_active:
        app_row.external_url = None
        session.add(app_row)
        
    session.commit()
    audit_log(user.username, "tunnel_unpublish", f"app '{app_row.name}' (id={app_id})")
    return {"ok": True}


@app.websocket("/api/apps/{app_id}/terminal")
async def terminal_ws(
    websocket: WebSocket,
    app_id: int,
    session: Session = Depends(get_session),
):
    await websocket.accept()

    token = websocket.cookies.get("session")
    if not token:
        await websocket.close(code=1008, reason="Not authenticated.")
        return

    db_session = session.get(UserSession, token)
    if not db_session:
        await websocket.close(code=1008, reason="Session invalid.")
        return

    if utc_now() > _make_tz_aware(db_session.expires_at):
        session.delete(db_session)
        session.commit()
        await websocket.close(code=1008, reason="Session expired.")
        return

    ws_user = session.get(User, db_session.user_id)
    if not ws_user:
        await websocket.close(code=1008, reason="User not found.")
        return

    try:
        await terminal_manager.start_terminal(
            websocket, app_id, session, db_session.user_id, ws_user.role
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

app.include_router(auth_router)
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
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=True,
    )