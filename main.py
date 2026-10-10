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

def _totp_verify(secret_b32: str, code: str, window: int = 1, after: Optional[int] = None) -> Optional[int]:
    """Return the time step *code* is valid for (current ± *window*), or None.

    Steps at or before *after* — the step of the last code this user was
    accepted with — don't count: a code is good for one use, so one that was
    shoulder-surfed or captured in transit can't be replayed inside the
    ~90 seconds it would otherwise stay valid (RFC 6238 §5.2)."""
    if not secret_b32 or not code:
        return None
    # Compared as bytes: compare_digest() raises TypeError on non-ASCII str,
    # which turned a mistyped code (e.g. full-width digits) into a 500.
    supplied = code.strip().encode("utf-8")
    now = int(time.time()) // 30
    for delta in range(-window, window + 1):
        if after is not None and now + delta <= after:
            continue
        expected = _totp_hotp(secret_b32, now + delta)
        if expected and hmac.compare_digest(expected.encode("ascii"), supplied):
            return now + delta
    return None

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
_fernet_lock = threading.Lock()


def _get_fernet() -> Fernet:
    """Return a process-wide Fernet instance, generating the local key on first run.

    The key never leaves this host, is never logged, and is never returned in
    any API response. Back it up alongside data.db (see module docstring).
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    # Request handlers run in a thread pool, so first use can be concurrent.
    # Unlocked, two threads could each generate a key: one encrypts with a key
    # the other then overwrites on disk, and that ciphertext is lost for good.
    with _fernet_lock:
        if _fernet_instance is None:
            if not _VENVY_KEY_FILE.exists():
                # Born 0600 under a private name, then linked into place: the
                # key is never readable by others (it used to be created with
                # the umask's mode and chmod'ed afterwards) and never exists
                # half-written. link() refuses to replace an existing key.
                tmp = _VENVY_KEY_FILE.with_name(f"{_VENVY_KEY_FILE.name}.{secrets.token_hex(4)}.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
                        f.flush()
                        os.fsync(f.fileno())
                    with contextlib.suppress(FileExistsError):
                        os.link(tmp, _VENVY_KEY_FILE)
                finally:
                    tmp.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                _VENVY_KEY_FILE.chmod(0o600)
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


# Peers allowed to tell us the real client IP via CF-Connecting-IP — i.e. where
# cloudflared connects from. Default: cloudflared running on this same host.
TRUSTED_PROXY_IPS: Set[str] = {
    ip.strip() for ip in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1").split(",") if ip.strip()
}


# Optional proof that a request really came through the tunnel. An address
# can't provide it: with the default TRUSTED_PROXY_IPS, cloudflared and every
# hosted app both connect from 127.0.0.1, so an app can send its own
# CF-Connecting-IP and be taken for any client it likes — past the IP
# allowlist, and past the per-IP login lockout. When this is set, the header
# is only believed on requests that also carry X-Venvy-Proxy-Secret with the
# same value (add it at the Cloudflare edge with a request-header transform
# rule). Hosted apps never see it: it is stripped from their environment.
PROXY_SECRET = os.getenv("VENVY_PROXY_SECRET", "")
# Venvy's own settings that must not leak into the processes it starts.
_PRIVATE_ENV_VARS = ("VENVY_PROXY_SECRET",)

if IP_ALLOWLIST is not None and not PROXY_SECRET and TRUSTED_PROXY_IPS & {"127.0.0.1", "::1"}:
    logging.warning(
        "IP_ALLOWLIST is set, but CF-Connecting-IP is trusted from loopback without "
        "VENVY_PROXY_SECRET: software running on this host (including hosted apps) "
        "can claim any client IP. Set VENVY_PROXY_SECRET to close that.")


def _resolve_client_ip(request: Union[Request, HTTPConnection]) -> str:
    """Resolve the real client IP.

    The TCP peer address is the only thing a client can't forge (uvicorn runs
    with proxy_headers=False, so X-Forwarded-For never rewrites it). When that
    peer is a trusted proxy — cloudflared on this host, by default — the
    request came through the tunnel and `CF-Connecting-IP`, which Cloudflare's
    edge sets and overwrites, names the actual client. From any other peer the
    header is ignored: a direct LAN/internet client could otherwise pick any
    IP it liked and walk past the IP allowlist and the login lockout.
    """
    peer = (request.client.host if request.client else "") or ""
    if peer in TRUSTED_PROXY_IPS:
        cf_ip = (request.headers.get("cf-connecting-ip", "") or "").strip()
        if cf_ip and (not PROXY_SECRET or secrets.compare_digest(
                (request.headers.get("x-venvy-proxy-secret", "") or "").encode("utf-8"),
                PROXY_SECRET.encode("utf-8"))):
            return cf_ip
    return peer or "unknown"


def check_ip_allowlist(request: HTTPConnection) -> None:
    """Dependency: reject requests from IPs not on the allowlist."""
    if IP_ALLOWLIST is None:
        return
    ip = _resolve_client_ip(request)
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
    totp_secret: Optional[str] = Field(default=None)   # None = TOTP disabled; encrypted at rest
    totp_enabled: bool = Field(default=False)
    # Time step of the last accepted code — see _totp_verify().
    totp_last_counter: Optional[int] = Field(default=None)


class UserSession(SQLModel, table=True):
    # SHA-256 of the session cookie, never the cookie itself — a leaked
    # data.db must not hand out live sessions.
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now)
    expires_at: datetime.datetime
    # SHA-256 of the per-session auth key the browser keeps in localStorage
    # and sends as X-Venvy-Key. See _authenticate().
    key_hash: Optional[str] = Field(default=None)


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
    # Start-time fingerprint of `pid` (see _proc_start_id). A PID alone can be
    # reused by an unrelated process after a crash or reboot; this lets Venvy
    # tell "our app is still running" from "something else got that number".
    pid_start: Optional[str] = Field(default=None)


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
        "ALTER TABLE app ADD COLUMN pid_start VARCHAR",
        "ALTER TABLE usersession ADD COLUMN key_hash VARCHAR",
        "ALTER TABLE user ADD COLUMN totp_last_counter INTEGER",
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

        # Sessions from before hashed tokens + auth keys can never validate
        # again; drop them rather than let them sit until expiry.
        conn.exec_driver_sql("DELETE FROM usersession WHERE key_hash IS NULL")

    # TOTP secrets used to be stored as-is; anyone who could read data.db (or a
    # backup of it) could generate every user's codes. Encrypt what's there.
    with Session(engine) as session:
        plain = [u for u in session.exec(select(User)).all()
                 if u.totp_secret and not u.totp_secret.startswith(_ENC_PREFIX)]
        for u in plain:
            u.totp_secret = _encrypt_secret(u.totp_secret)
            session.add(u)
        if plain:
            session.commit()
            logging.info(f"Encrypted {len(plain)} stored TOTP secret(s).")

    APPS_BASE_DIR.mkdir(exist_ok=True)
    _lock_down_data_files()


def _lock_down_data_files() -> None:
    """Make Venvy's own state readable by the Venvy user only."""
    for path, mode in ((DB_FILE, 0o600), (_VENVY_KEY_FILE, 0o600), (BACKUP_DIR, 0o700),
                       (APP_LOGS_DIR, 0o700), (_UPLOAD_TMP_DIR, 0o700)):
        try:
            if path.exists():
                path.chmod(mode)
        except OSError:
            pass
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(DB_FILE) + suffix).chmod(0o600)
        except OSError:
            pass


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


# Checked when a login names a user that doesn't exist, so that case costs the
# same scrypt run as a wrong password and response time doesn't reveal which
# usernames are real.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise HTTPException(status_code=400, detail="Password must contain both letters and numbers.")


def _make_tz_aware(dt: datetime.datetime) -> datetime.datetime:
    """Return *dt* with UTC tzinfo attached if it is naive."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_session(session: Session, response: Response, user: User) -> str:
    """Create a session for *user*: set the HttpOnly cookie and return the
    auth key the client must send back as X-Venvy-Key on every request.

    Why two secrets: browsers send cookies to every port on a host, so the
    session cookie also reaches each hosted app on http://<same-host>:<port>
    the moment you open it. The auth key lives in localStorage, which is
    scoped per origin (port included) and never sent automatically — a
    cookie captured by a hosted app is useless without it."""
    token = secrets.token_urlsafe(32)
    auth_key = secrets.token_urlsafe(32)
    session.add(UserSession(
        token=_hash_token(token),
        user_id=user.id,
        expires_at=utc_now() + datetime.timedelta(seconds=SESSION_TTL_SECONDS),
        key_hash=_hash_token(auth_key),
    ))
    session.commit()
    response.set_cookie(
        "session", token, httponly=True, path="/",
        max_age=SESSION_TTL_SECONDS, samesite="strict", secure=SESSION_COOKIE_SECURE,
    )
    return auth_key


def _load_session(session: Session, cookie_token: Optional[str]) -> Optional[UserSession]:
    """Return the live UserSession for a cookie value, sliding its expiry."""
    if not cookie_token:
        return None
    db_session = session.get(UserSession, _hash_token(cookie_token))
    if not db_session:
        return None
    now = utc_now()
    if now > _make_tz_aware(db_session.expires_at):
        session.delete(db_session)
        session.commit()
        return None
    # Slide expiry window if less than 7 days remain.
    remaining = (_make_tz_aware(db_session.expires_at) - now).total_seconds()
    if remaining < 60 * 60 * 24 * 7:
        db_session.expires_at = now + datetime.timedelta(seconds=SESSION_TTL_SECONDS)
        session.add(db_session)
        session.commit()
    return db_session


def _authenticate(session: Session, cookie_token: Optional[str], auth_key: Optional[str]) -> Optional[User]:
    db_session = _load_session(session, cookie_token)
    if not db_session or not db_session.key_hash or not auth_key:
        return None
    if not secrets.compare_digest(db_session.key_hash, _hash_token(auth_key)):
        return None
    return session.get(User, db_session.user_id)


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = _authenticate(session, request.cookies.get("session"), request.headers.get("x-venvy-key"))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user


# One-time download tickets: a plain <a href download> link can't carry the
# X-Venvy-Key header, so the page trades its key for a ticket that works for
# one GET within a minute. {ticket_hash: (user_id, expires_monotonic)}
_download_tickets: Dict[str, tuple] = {}
_download_tickets_lock = threading.Lock()
_DOWNLOAD_TICKET_TTL = 60


def get_download_user(request: Request, session: Session = Depends(get_session)) -> User:
    """get_current_user for download endpoints: also accepts ?ticket=."""
    ticket = request.query_params.get("ticket")
    if not ticket:
        return get_current_user(request, session)
    now = time.monotonic()
    with _download_tickets_lock:
        for k in [k for k, (_, exp) in _download_tickets.items() if exp < now]:
            _download_tickets.pop(k, None)
        entry = _download_tickets.pop(_hash_token(ticket), None)
    user = session.get(User, entry[0]) if entry else None
    # The ticket must also come with this browser's session cookie.
    db_session = _load_session(session, request.cookies.get("session"))
    if not user or not db_session or db_session.user_id != user.id:
        raise HTTPException(status_code=401, detail="Download link expired — try again.")
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


# Files Venvy itself writes into an app directory. Users must not be able to
# overwrite or delete them through the file manager.
_PROTECTED_APP_FILE_PREFIXES = (".jailrc", ".jailsh", ".bashrc-venvy")


def _is_protected_app_file(name: str) -> bool:
    return name.startswith(_PROTECTED_APP_FILE_PREFIXES)


def _upload_rel_path_error(rel_path: str) -> Optional[str]:
    """Return why *rel_path* (a browser-supplied relative upload path, possibly
    containing folders) is unacceptable, or None if it's fine. Containment
    inside the app dir is still checked separately with is_safe_path()."""
    if not rel_path or rel_path.startswith("/") or "\\" in rel_path:
        return "invalid path"
    parts = rel_path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return "invalid path"
        if any(ord(c) < 32 or ord(c) == 127 for c in part):
            return "name contains control characters"
        if len(part.encode("utf-8")) > 255:
            return "name is too long"
    if _is_protected_app_file(parts[-1]):
        return "reserved filename"
    return None


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
    for _private in _PRIVATE_ENV_VARS:
        env.pop(_private, None)
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
# Per-app Unix users (opt-in: VENVY_APP_USERS=1, Linux, Venvy running as root)
# ---------------------------------------------------------------------------
# Without this, every hosted app, terminal and pip run shares Venvy's own OS
# user and can read data.db, .venvy_key and every other app. With it, each
# app gets a locked system user `venvy-app-<id>` that owns only its own app
# directory; its process, terminal and package installs run as that user.
# Anything Venvy itself writes as root on an app's behalf (logs, the terminal
# rc file) lives outside the app directory, so an app can't redirect those
# writes with a symlink.
_APP_USERS_REQUESTED = os.getenv("VENVY_APP_USERS", "0") == "1"
_APP_USERS_ACTIVE: Optional[bool] = None
TERMINAL_RC_DIR = DATA_DIR / ".terminal"
APP_LOGS_DIR = DATA_DIR / "app-logs"


def _isolation_active() -> bool:
    global _APP_USERS_ACTIVE
    if _APP_USERS_ACTIVE is None:
        ok = _APP_USERS_REQUESTED
        if ok and not sys.platform.startswith("linux"):
            logging.error("VENVY_APP_USERS=1 is only supported on Linux — running without per-app users.")
            ok = False
        if ok and os.geteuid() != 0:
            logging.error("VENVY_APP_USERS=1 needs Venvy to run as root — running without per-app users.")
            ok = False
        if ok and not (shutil.which("useradd") and shutil.which("userdel")):
            logging.error("VENVY_APP_USERS=1 needs useradd/userdel — running without per-app users.")
            ok = False
        _APP_USERS_ACTIVE = ok
        if ok:
            logging.info("Per-app Unix users: ON")
    return _APP_USERS_ACTIVE


def _app_username(app_id: int) -> str:
    return f"venvy-app-{app_id}"


def _app_ids(app_row: "App") -> Optional[tuple]:
    """(uid, gid) of the app's user, or None when isolation is off."""
    if not _isolation_active() or app_row.id is None:
        return None
    import pwd
    try:
        pw = pwd.getpwnam(_app_username(app_row.id))
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    """Give *path* and everything under it to uid:gid without ever following
    a symlink. The tree belongs to the app, which can rearrange it while we
    walk: os.fwalk() hands out a directory fd per level and verifies each
    directory it enters is the one it listed, so a directory swapped for a
    symlink mid-walk is skipped instead of leading root's chown elsewhere."""
    os.lchown(path, uid, gid)
    if path.is_dir() and not path.is_symlink():
        for _dirpath, dirs, files, dirfd in os.fwalk(path, follow_symlinks=False):
            for n in dirs + files:
                with contextlib.suppress(FileNotFoundError):
                    os.chown(n, uid, gid, dir_fd=dirfd, follow_symlinks=False)


def _ensure_app_user(app_row: "App") -> Optional[tuple]:
    """Create the app's user if needed and make sure it owns its directory.
    Blocking; returns (uid, gid) or None when isolation is off."""
    if not _isolation_active():
        return None
    name = _app_username(app_row.id)
    ids = _app_ids(app_row)
    if ids is None:
        subprocess.run(
            ["useradd", "--system", "--user-group", "--no-create-home",
             "--home-dir", app_row.app_dir, "--shell", "/usr/sbin/nologin", name],
            check=True, capture_output=True, timeout=30,
        )
        ids = _app_ids(app_row)
        if ids is None:
            raise RuntimeError(f"Could not create user {name}")
    uid, gid = ids
    app_dir = Path(app_row.app_dir)
    # Parents: traversable, not listable, so apps can't enumerate each other.
    for parent in (APPS_BASE_DIR, app_dir.parent):
        with contextlib.suppress(OSError):
            parent.chmod(0o711)
    if app_dir.exists() and app_dir.stat().st_uid != uid:
        _chown_tree(app_dir, uid, gid)
    with contextlib.suppress(OSError):
        app_dir.chmod(0o700)
    return ids


def _delete_app_user(app_id: int) -> None:
    if not _isolation_active():
        return
    with contextlib.suppress(Exception):
        subprocess.run(["userdel", _app_username(app_id)], capture_output=True, timeout=30)


def _chown_app_dir(app_row: "App") -> None:
    """Hand the app's whole directory (back) to its user, after a tool that
    ran on its behalf may have left other owners behind."""
    ids = _app_ids(app_row)
    app_dir = Path(app_row.app_dir)
    if ids and app_dir.exists():
        _chown_tree(app_dir, *ids)


# ---------------------------------------------------------------------------
# File access inside app directories
# ---------------------------------------------------------------------------
# With per-app users Venvy is root and the directory belongs to the app, which
# can change it at any moment — including swapping a directory for a symlink
# between Venvy checking a path and using it. A path string that was safe when
# checked is therefore worth nothing by the time it is opened: root would
# follow the new link and write (or read, or chown) wherever it points.
#
# So nothing below uses a path twice. A request path is resolved once (which
# is what lets an app's own, legitimate symlinks keep working), reduced to its
# components under the app directory, and then walked from the app directory
# one component at a time with O_NOFOLLOW. Every operation happens relative to
# the directory fd that walk returns. If anything on the way has turned into a
# symlink since it was resolved, the walk fails instead of following it.

_O_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_FS_COPY_BUF = 1024 * 1024


class AppPathError(Exception):
    """A path inside an app directory can't be used as asked. Carries the
    HTTP status the API should answer with."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _app_rel_parts(root: Path, rel_path: str, follow_last: bool = True) -> tuple:
    """Check a client-supplied path against the app directory *root* and
    return its components below it. With follow_last=False the final
    component is taken literally rather than resolved, for operations that
    act on a directory entry itself (delete, rename): deleting a symlink
    should remove the link, not what it points to."""
    root_r = root.resolve()
    cand = root / (rel_path or "")
    if follow_last or cand.name in ("", ".", ".."):
        target = cand.resolve()
    else:
        target = cand.parent.resolve() / cand.name
    if not (target == root_r or root_r in target.parents):
        raise AppPathError(403, "Path is outside the app directory.")
    return target.relative_to(root_r).parts


@contextlib.contextmanager
def _open_app_dir(root: Path, parts: tuple, create: bool = False, owner: Optional[tuple] = None):
    """Yield a directory fd for root/parts, reached without following any
    symlink below root. create=True makes missing directories on the way
    (owned by *owner* when given)."""
    fd = os.open(root.resolve(), _O_NOFOLLOW_DIR)
    try:
        for part in parts:
            try:
                try:
                    nfd = os.open(part, _O_NOFOLLOW_DIR, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise AppPathError(404, f"'{part}' does not exist.")
                    with contextlib.suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                    nfd = os.open(part, _O_NOFOLLOW_DIR, dir_fd=fd)
                    if owner:
                        os.fchown(nfd, *owner)
            except OSError as exc:
                # ELOOP / ENOTDIR: a symlink or a plain file where a folder
                # was expected.
                raise AppPathError(409, f"'{part}' is not a folder.") from exc
            os.close(fd)
            fd = nfd
        yield fd
    finally:
        os.close(fd)


def _fs_lstat(dir_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _fs_is_dir(st: Optional[os.stat_result]) -> bool:
    import stat as _stat
    return st is not None and _stat.S_ISDIR(st.st_mode)


def _fs_write_new(dir_fd: int, name: str, chunks, mode: int, owner: Optional[tuple], limit: Optional[int] = None) -> bool:
    """Write *chunks* to dir_fd/name via a temp file + rename, so a crash or
    full disk never leaves a truncated file behind. Returns False (nothing
    changed) if the data exceeds *limit* bytes."""
    tmp = f".{name}.venvy-{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        written = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in chunks:
                written += len(chunk)
                if limit is not None and written > limit:
                    return False
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
            os.fchmod(f.fileno(), mode)
            if owner:
                os.fchown(f.fileno(), *owner)
        # rename() replaces a symlink itself, it never writes through one.
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return True
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp, dir_fd=dir_fd)


def _fs_read_chunks(fileobj):
    while True:
        buf = fileobj.read(_FS_COPY_BUF)
        if not buf:
            return
        yield buf


def _fs_save(root: Path, parts: tuple, data: bytes, owner: Optional[tuple]) -> None:
    """Create or overwrite the file root/parts, keeping an existing file's mode."""
    import stat as _stat
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        st = _fs_lstat(dfd, parts[-1])
        if _fs_is_dir(st):
            raise AppPathError(409, "That path is a folder.")
        mode = _stat.S_IMODE(st.st_mode) if st is not None and _stat.S_ISREG(st.st_mode) else 0o644
        _fs_write_new(dfd, parts[-1], [data], mode, owner)


def _fs_write_stream(root: Path, parts: tuple, fileobj, limit: int, owner: Optional[tuple]) -> bool:
    """Stream *fileobj* into root/parts. False if it is larger than *limit*."""
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        if _fs_is_dir(_fs_lstat(dfd, parts[-1])):
            raise AppPathError(409, f"'{parts[-1]}' already exists as a folder.")
        return _fs_write_new(dfd, parts[-1], _fs_read_chunks(fileobj), 0o644, owner, limit)


def _fs_install(root: Path, parts: tuple, src: Path, owner: Optional[tuple]) -> None:
    """Move *src* — a finished file in Venvy's own temp space — to root/parts."""
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        if _fs_is_dir(_fs_lstat(dfd, parts[-1])):
            raise AppPathError(409, f"'{parts[-1]}' already exists as a folder.")
        if owner:
            os.lchown(src, *owner)
        try:
            os.replace(src, parts[-1], dst_dir_fd=dfd)
        except OSError:
            # Different filesystem (e.g. DATA_DIR/apps is a mount) — copy instead.
            mode = os.stat(src).st_mode & 0o777
            with open(src, "rb") as f:
                _fs_write_new(dfd, parts[-1], _fs_read_chunks(f), mode, owner)
            src.unlink(missing_ok=True)


def _fs_touch(root: Path, parts: tuple, owner: Optional[tuple]) -> None:
    with _open_app_dir(root, parts[:-1], create=True, owner=owner) as dfd:
        try:
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o644, dir_fd=dfd)
        except FileExistsError:
            # Same as touch(1) on an existing entry: just bump its mtime.
            os.utime(parts[-1], dir_fd=dfd, follow_symlinks=False)
            return
        try:
            if owner:
                os.fchown(fd, *owner)
        finally:
            os.close(fd)


def _fs_mkdir(root: Path, parts: tuple, owner: Optional[tuple]) -> None:
    with _open_app_dir(root, parts, create=True, owner=owner):
        pass


def _fs_rmtree(dir_fd: int, name: str) -> None:
    """Remove the directory dir_fd/name and everything in it, best effort,
    descending only into real directories (never through a symlink)."""
    try:
        sub = os.open(name, _O_NOFOLLOW_DIR, dir_fd=dir_fd)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
        return
    try:
        with os.scandir(sub) as it:
            entries = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        for child, is_dir in entries:
            if is_dir:
                _fs_rmtree(sub, child)
            else:
                with contextlib.suppress(OSError):
                    os.unlink(child, dir_fd=sub)
    finally:
        os.close(sub)
    with contextlib.suppress(OSError):
        os.rmdir(name, dir_fd=dir_fd)


def _fs_delete(root: Path, parts: tuple) -> None:
    with _open_app_dir(root, parts[:-1]) as dfd:
        st = _fs_lstat(dfd, parts[-1])
        if st is None:
            return
        if _fs_is_dir(st):
            _fs_rmtree(dfd, parts[-1])
        else:
            os.unlink(parts[-1], dir_fd=dfd)


def _fs_copy_entry(src_fd: int, src_name: str, dst_fd: int, dst_name: str, owner: Optional[tuple]) -> None:
    """Copy one directory entry (file, symlink or whole directory) between
    two directory fds. Symlinks are copied as symlinks, never followed."""
    import stat as _stat
    st = os.stat(src_name, dir_fd=src_fd, follow_symlinks=False)
    if _stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src_name, dir_fd=src_fd), dst_name, dir_fd=dst_fd)
        if owner:
            os.chown(dst_name, *owner, dir_fd=dst_fd, follow_symlinks=False)
    elif _stat.S_ISDIR(st.st_mode):
        os.mkdir(dst_name, 0o700, dir_fd=dst_fd)
        s_sub = os.open(src_name, _O_NOFOLLOW_DIR, dir_fd=src_fd)
        try:
            d_sub = os.open(dst_name, _O_NOFOLLOW_DIR, dir_fd=dst_fd)
            try:
                if owner:
                    os.fchown(d_sub, *owner)
                with os.scandir(s_sub) as it:
                    names = [e.name for e in it]
                for name in names:
                    _fs_copy_entry(s_sub, name, d_sub, name, owner)
                os.fchmod(d_sub, _stat.S_IMODE(st.st_mode))
            finally:
                os.close(d_sub)
        finally:
            os.close(s_sub)
    elif _stat.S_ISREG(st.st_mode):
        fd = os.open(src_name, os.O_RDONLY | os.O_NOFOLLOW | _O_CLOEXEC, dir_fd=src_fd)
        with os.fdopen(fd, "rb") as f:
            out = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | _O_CLOEXEC, 0o600, dir_fd=dst_fd)
            with os.fdopen(out, "wb") as o:
                shutil.copyfileobj(f, o, _FS_COPY_BUF)
                os.fchmod(o.fileno(), _stat.S_IMODE(st.st_mode))
                if owner:
                    os.fchown(o.fileno(), *owner)
        os.utime(dst_name, ns=(st.st_atime_ns, st.st_mtime_ns), dir_fd=dst_fd, follow_symlinks=False)
    # Sockets, FIFOs and devices are not copied.


def _fs_move_or_copy(root: Path, src: tuple, dst: tuple, copy: bool, owner: Optional[tuple], dst_label: str) -> None:
    """Rename (copy=False) or copy the entry root/src to root/dst."""
    with _open_app_dir(root, src[:-1]) as sfd, _open_app_dir(root, dst[:-1], create=True, owner=owner) as dfd:
        s_st = _fs_lstat(sfd, src[-1])
        if s_st is None:
            raise AppPathError(404, "Source does not exist.")
        d_st = _fs_lstat(dfd, dst[-1])
        # The one overwrite allowed: renaming an entry onto itself, which is
        # how a case-only rename looks on a case-insensitive filesystem.
        same = d_st is not None and (d_st.st_dev, d_st.st_ino) == (s_st.st_dev, s_st.st_ino)
        if d_st is not None and not (same and not copy):
            raise AppPathError(409, f"'{dst_label}' already exists.")
        if copy:
            _fs_copy_entry(sfd, src[-1], dfd, dst[-1], owner)
        else:
            os.rename(src[-1], dst[-1], src_dir_fd=sfd, dst_dir_fd=dfd)


def _fs_open_file(root: Path, parts: tuple):
    """Open the regular file root/parts for reading; returns (file object,
    stat). Raises AppPathError(404) for anything else."""
    import stat as _stat
    if not parts:
        raise AppPathError(404, "File not found.")
    with _open_app_dir(root, parts[:-1]) as dfd:
        try:
            # O_NONBLOCK: don't hang on a FIFO that appeared in the meantime.
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC, dir_fd=dfd)
        except OSError:
            raise AppPathError(404, "File not found.")
    st = os.fstat(fd)
    if not _stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise AppPathError(404, "File not found.")
    os.set_blocking(fd, True)
    return os.fdopen(fd, "rb"), st


def _fs_listdir(root: Path, parts: tuple) -> List[dict]:
    """[{name, is_dir, size}] for the directory root/parts ([] if it isn't one)."""
    try:
        with _open_app_dir(root, parts) as dfd:
            out = []
            with os.scandir(dfd) as it:
                for entry in it:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        size = 0
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    out.append({"name": entry.name, "is_dir": is_dir, "size": size})
            return out
    except AppPathError:
        return []


def _file_download_response(fileobj, st: os.stat_result, filename: str, media_type: Optional[str] = None):
    """What FileResponse(path, filename=...) would send, but from a file that
    is already open — the path is not looked up a second time."""
    import mimetypes
    from starlette.responses import StreamingResponse

    def _iter():
        with fileobj:
            yield from _fs_read_chunks(fileobj)

    quoted = urllib.parse.quote(filename)
    disposition = (f'attachment; filename="{filename}"' if quoted == filename
                   else f"attachment; filename*=utf-8''{quoted}")
    return StreamingResponse(
        _iter(),
        media_type=media_type or mimetypes.guess_type(filename)[0] or "text/plain",
        headers={
            "Content-Length": str(st.st_size),
            "Content-Disposition": disposition,
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


def _app_proc_kwargs(app_row: "App") -> dict:
    """subprocess kwargs that run a command as the app's user (or nothing)."""
    ids = _app_ids(app_row)
    if not ids:
        return {}
    return {"user": ids[0], "group": ids[1], "extra_groups": []}


def _app_tool_env(app_row: "App") -> Dict[str, str]:
    """Environment for venv/pip/cargo runs on an app's behalf."""
    env = os.environ.copy()
    for _private in _PRIVATE_ENV_VARS:
        env.pop(_private, None)
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    if _app_ids(app_row):
        env.update(HOME=app_row.app_dir, USER=_app_username(app_row.id), LOGNAME=_app_username(app_row.id))
    return env


def _app_log_file(app_row: "App") -> Path:
    """Where the app's log lives. Under isolation it's outside the app dir —
    Venvy writes it as root, and the app owns everything under app_dir."""
    if _isolation_active():
        return APP_LOGS_DIR / str(app_row.id) / "app.log"
    return Path(app_row.app_dir) / "logs" / "app.log"


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
# Creating an app (venv + pip), installing packages and building/restoring
# backups can take minutes. Cloudflare cuts proxied requests off at 100 s, so
# these endpoints start a job and answer {"job_id"} at once; the client polls
# GET /api/jobs/{id}.
_jobs: Dict[str, dict] = {}
_JOB_TTL_SEC = 3600


def _start_job(user: "User", kind: str, fn) -> dict:
    """Run coroutine function *fn* in the background; return {"job_id"}."""
    now = time.time()
    for jid in [j for j, v in _jobs.items() if v["status"] != "running" and now - v["updated"] > _JOB_TTL_SEC]:
        _jobs.pop(jid, None)
    job_id = secrets.token_hex(16)
    job = {"id": job_id, "kind": kind, "user_id": user.id, "status": "running",
           "result": None, "error": None, "code": None, "created": now, "updated": now}
    _jobs[job_id] = job

    async def _run():
        try:
            job["result"] = await fn()
            job["status"] = "done"
        except HTTPException as exc:
            job.update(status="error", error=exc.detail, code=exc.status_code)
        except Exception as exc:
            logging.error(f"Job {kind} failed: {exc}")
            job.update(status="error", error=str(exc) or exc.__class__.__name__, code=500)
        finally:
            job["updated"] = time.time()
            _running_tasks.discard(asyncio.current_task())

    t = asyncio.create_task(_run())
    _running_tasks.add(t)
    return {"job_id": job_id}


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


async def _finish_log_task(app_id: int, timeout: float = 2.0) -> None:
    """Let the app's log streamer drain what the process wrote while it was
    shutting down — its pipe reaches EOF once the process is gone — then make
    sure the task is finished either way."""
    task = _log_tasks.pop(app_id, None)
    if not task or task.done():
        return
    await asyncio.wait({task}, timeout=timeout)
    if not task.done():
        task.cancel()


_LOG_MAX_BYTES = int(os.getenv("VENVY_LOG_MAX_MB", "20")) * 1024 * 1024
_LOG_KEEP_ROTATED = 5
_LOG_MAX_LINE = 64 * 1024


def _rotate_log(log_file: Path) -> None:
    """Move app.log aside as app.log.<ts> and keep only the newest few."""
    target = log_file.with_name(f"{log_file.name}.{time.time_ns()}")
    shutil.move(str(log_file), str(target))
    rotated = sorted(log_file.parent.glob(f"{log_file.name}.*"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old_log in rotated[_LOG_KEEP_ROTATED:]:
        try:
            old_log.unlink()
        except OSError:
            pass


async def log_streamer(stream, log_file: Path) -> None:
    """Copy *stream* into *log_file* line by line with timestamps, rotating
    the file whenever it passes _LOG_MAX_BYTES.

    Reads raw chunks instead of StreamReader.readline(): readline() raises on
    any line over 64 KB, and if this loop dies nothing drains the app's stdout
    pipe any more — the app then blocks forever on its next print(). Over-long
    lines are split instead."""
    pending = b""
    f = None
    try:
        f = await aiofiles.open(log_file, "ab")
        while True:
            chunk = await stream.read(65536)
            if chunk:
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                while len(pending) > _LOG_MAX_LINE:
                    lines.append(pending[:_LOG_MAX_LINE])
                    pending = pending[_LOG_MAX_LINE:]
            else:
                lines = [pending] if pending else []
                pending = b""
            if lines:
                ts = utc_now().strftime("%Y-%m-%d %H:%M:%S").encode()
                await f.write(b"".join(b"[" + ts + b"] " + ln + b"\n" for ln in lines))
                await f.flush()
                try:
                    too_big = os.stat(log_file).st_size > _LOG_MAX_BYTES
                except OSError:
                    too_big = False
                if too_big:
                    await f.close()
                    try:
                        await asyncio.to_thread(_rotate_log, log_file)
                    except Exception as exc:
                        logging.warning(f"Log rotation failed: {exc}")
                    f = await aiofiles.open(log_file, "ab")
            if not chunk:
                break
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logging.error(f"Log streamer error: {exc}")
        # Keep draining so the app never blocks on a full pipe.
        with contextlib.suppress(Exception):
            while await stream.read(65536):
                pass
    finally:
        if f is not None:
            with contextlib.suppress(Exception):
                await f.close()


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


def _proc_start_id(pid: int) -> Optional[str]:
    """A fingerprint of when *pid* started, or None if unknown. Two processes
    that happen to share a PID (one exited, the number was reused) differ."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return "proc:" + stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    if psutil:
        try:
            return f"ps:{psutil.Process(pid).create_time():.2f}"
        except Exception:
            pass
    return None


def _app_pid_alive(app_row: "App") -> bool:
    """True only if app_row.pid is alive AND is still the process Venvy started."""
    if not app_row.pid or not _pid_alive(app_row.pid):
        return False
    if app_row.pid_start:
        return _proc_start_id(app_row.pid) == app_row.pid_start
    return True  # recorded before fingerprints existed — can't tell


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


# One lock per app serialises start/stop/restart. Without it a manual start
# racing the supervisor's automatic restart (or a double click) spawned two
# processes, and only the second was tracked — the first ran on unmanaged.
_app_locks: Dict[int, asyncio.Lock] = {}


def _app_lock(app_id: int) -> asyncio.Lock:
    return _app_locks.setdefault(app_id, asyncio.Lock())


async def start_app_process_logic(app_id: int, session: Session) -> App:
    async with _app_lock(app_id):
        return await _start_app_process(app_id, session)


async def _start_app_process(app_id: int, session: Session) -> App:
    """Start the app. Caller holds _app_lock(app_id)."""
    # populate_existing: the caller's session may hold a copy loaded before
    # we got the lock.
    db_app = session.get(App, app_id, populate_existing=True)
    if not db_app:
        raise ValueError("App not found.")

    if db_app.pid and await asyncio.to_thread(_app_pid_alive, db_app):
        db_app.status = "running"
        session.add(db_app)
        session.commit()
        logging.info(f"start_app: '{db_app.name}' already running (PID {db_app.pid})")
        return db_app

    db_app.pid = None
    db_app.pid_start = None

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
    ids = await asyncio.to_thread(_ensure_app_user, db_app)
    log_file = _app_log_file(db_app)
    if ids:
        # Venvy-only: apps must not read each other's logs.
        APP_LOGS_DIR.mkdir(mode=0o700, exist_ok=True)
        APP_LOGS_DIR.chmod(0o700)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    if log_file.exists() and log_file.stat().st_size > 5 * 1024 * 1024:
        try:
            await asyncio.to_thread(_rotate_log, log_file)
        except Exception as exc:
            logging.warning(f"Log rotation failed: {exc}")

    if not await asyncio.to_thread(log_file.exists):
        await asyncio.to_thread(log_file.touch)

    env = _build_app_env(db_app)
    if ids:
        env.update(HOME=str(app_dir), USER=_app_username(app_id), LOGNAME=_app_username(app_id))
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
            if ids:
                scope_cmd += [f"--uid={ids[0]}", f"--gid={ids[1]}"]
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
            # Under systemd-run, --uid/--gid above does the switch (it must
            # start as root to create the scope); otherwise switch here.
            **({} if used_systemd_scope else _app_proc_kwargs(db_app)),
        )

        if process.stdout:
            task = asyncio.create_task(log_streamer(process.stdout, log_file))
            _log_tasks[app_id] = task

        db_app.pid = process.pid
        db_app.pid_start = await asyncio.to_thread(_proc_start_id, process.pid)
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
                            app_to_reap.pid_start = None
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
                # Only forget our own PID: after a restart this slot already
                # holds the replacement process.
                if _active_app_pids.get(_app_id) == proc.pid:
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
    async with _app_lock(app_id):
        await _stop_app_process(app_id, session, manual)


async def _stop_app_process(app_id: int, session: Session, manual: bool = False) -> None:
    """Stop the app. Caller holds _app_lock(app_id)."""
    db_app = session.get(App, app_id, populate_existing=True)
    if not db_app:
        _cancel_log_task(app_id)
        return
    # Recorded even when nothing is running: that is exactly the state of a
    # crash-looping app between two automatic restarts, and Stop has to end
    # the loop there too.
    if manual:
        db_app.last_manually_stopped_at = utc_now()
    if not db_app.pid:
        _cancel_log_task(app_id)
        db_app.status = "stopped"
        session.add(db_app)
        session.commit()
        return

    pid = db_app.pid
    if not await asyncio.to_thread(_app_pid_alive, db_app):
        # Already gone — or the PID now belongs to an unrelated process,
        # which must not be killed. Just record the app as stopped.
        logging.info(f"App '{db_app.name}' is not running (PID {pid} gone or reused)")
        await _finish_log_task(app_id)
        db_app.status = "stopped"
        db_app.pid = None
        db_app.pid_start = None
        db_app.systemd_scope = None
        db_app.last_stop_time = utc_now()
        session.add(db_app)
        session.commit()
        return
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
    # Only now: cancelling the streamer before the kill (as this used to)
    # threw away everything the app printed while handling SIGTERM.
    await _finish_log_task(app_id)

    db_app.status = "stopped"
    db_app.pid = None
    db_app.pid_start = None
    db_app.systemd_scope = None
    db_app.last_stop_time = utc_now()
    db_app.last_exit_code = -15

    session.add(db_app)
    session.commit()


# Crash-loop back-off for automatic restarts: 5s, 10s, 20s … capped at 5 min,
# reset once an app has stayed up for _RESTART_STABLE_SEC. {app_id: (failures, not_before)}
_restart_backoff: Dict[int, tuple] = {}
_RESTART_STABLE_SEC = 60
_RESTART_MAX_DELAY = 300


def _restart_allowed(app_id: int) -> bool:
    """Return True (and book the attempt) if an automatic restart may run now."""
    failures, not_before = _restart_backoff.get(app_id, (0, 0.0))
    now = time.monotonic()
    if now < not_before:
        return False
    delay = min(5 * (2 ** failures), _RESTART_MAX_DELAY)
    _restart_backoff[app_id] = (failures + 1, now + delay)
    if failures >= 3:
        logging.warning(f"App {app_id} keeps crashing — next automatic restart in {delay}s at the earliest.")
    return True


# Set while a restore is running (from the request until the process re-execs,
# or until the restore fails). Starting apps is held off for the duration.
_restore_in_progress = False


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

            if _restore_in_progress:
                # The restored data.db describes apps as they were when the
                # backup was taken — "running", with PIDs long gone. Acting on
                # that here would start them moments before Venvy re-execs,
                # cutting off their output pipe and leaving them unsupervised.
                continue

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
                _purge_login_attempts()

            apps_to_check: List[App] = []
            try:
                with Session(engine) as session:
                    apps_to_check = session.exec(select(App)).all()
            except Exception as e:
                logging.error(f"Failed to fetch apps: {e}")
                await asyncio.sleep(5)
                continue

            for stale_row in apps_to_check:
                # This loop awaits (restarts, port scans), so the snapshot above
                # can be seconds old by the time a given app is reached.
                try:
                    with Session(engine) as session:
                        app_row = session.get(App, stale_row.id)
                except Exception as e:
                    logging.error(f"Failed to fetch app {stale_row.id}: {e}")
                    continue
                if app_row is None:
                    continue
                is_alive = _app_pid_alive(app_row)

                if app_row.status == "running" and not is_alive:
                    died = False
                    async with _app_lock(app_row.id):
                        try:
                            with Session(engine) as update_session:
                                refreshed = update_session.get(App, app_row.id)
                                # Re-check under the lock: a restart may have
                                # replaced this PID while we waited, and its
                                # new process must not be written off as dead.
                                if (refreshed and refreshed.status == "running"
                                        and refreshed.pid == app_row.pid):
                                    refreshed.pid = None
                                    refreshed.pid_start = None
                                    refreshed.systemd_scope = None
                                    refreshed.status = "stopped"
                                    refreshed.last_stop_time = utc_now()
                                    if refreshed.last_exit_code is None:
                                        refreshed.last_exit_code = 1
                                    update_session.add(refreshed)
                                    update_session.commit()
                                    died = True
                        except Exception as db_exc:
                            logging.error(f"Failed to update state for {app_row.name}: {db_exc}")
                    if not died:
                        continue
                    logging.warning(f"App '{app_row.name}' (PID {app_row.pid}) died unexpectedly.")

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
                        if not was_manual and _restart_allowed(app_row.id):
                            logging.info(f"Restarting '{app_row.name}' (policy: {app_row.restart_policy})")
                            try:
                                with Session(engine) as restart_session:
                                    await start_app_process_logic(app_row.id, restart_session)
                            except Exception as exc:
                                logging.error(f"Restart failed for '{app_row.name}': {exc}")

                elif app_row.status == "running" and is_alive:
                    if (app_row.last_start_time is not None
                            and (utc_now() - _make_tz_aware(app_row.last_start_time)).total_seconds() > _RESTART_STABLE_SEC):
                        _restart_backoff.pop(app_row.id, None)
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
                        if not was_manual and _restart_allowed(app_row.id):
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

def _write_bashrc(app_id: int, jail_root: Path, venv_bin: Path, app_name_safe: str) -> Path:
    """Write the terminal's rc file. It lives in Venvy's own directory, not the
    app's: the app can't tamper with it, and Venvy never writes (as root,
    under per-app users) to a path the app controls. The cd "jail" in it is a
    convenience only — the real boundary is the per-app user, when enabled."""
    TERMINAL_RC_DIR.mkdir(mode=0o755, exist_ok=True)
    bashrc_path = TERMINAL_RC_DIR / f"app-{app_id}.bashrc"
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
        user_id: int,
        user_role: str,
    ) -> None:
        if user_role not in ("admin", "operator"):
            await websocket.close(code=1008, reason="Operator or admin privileges required for terminal access.")
            return
        # Read the app and let go of the DB straight away — see terminal_ws().
        with Session(engine) as session:
            db_app = session.get(App, app_id)
        if not db_app:
            await websocket.close(code=1008, reason="App not found.")
            return

        jail_root = Path(db_app.app_dir).resolve()
        venv_bin  = Path(db_app.venv_path) / "bin"

        _app_name_safe = re.sub(r"[^A-Za-z0-9_.+\-]", "-", db_app.name)[:24].strip("-") or "app"

        try:
            bashrc_script = _write_bashrc(app_id, jail_root, venv_bin, _app_name_safe)
            term_ids = await asyncio.to_thread(_ensure_app_user, db_app)
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
        if term_ids:
            _raw_env.update(HOME=str(jail_root), USER=_app_username(app_id), LOGNAME=_app_username(app_id))
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

                    # Close everything else inherited from Venvy — its listening
                    # socket, other users' HTTP connections, the DB. A shell (or
                    # anything started from it) must not be able to accept()
                    # Venvy's traffic or keep its port bound after it exits.
                    try:
                        max_fd = os.sysconf("SC_OPEN_MAX")
                    except Exception:
                        max_fd = 4096
                    os.closerange(3, max_fd)

                    if term_ids:
                        # Drop to the app's own user before anything runs.
                        os.setgroups([])
                        os.setgid(term_ids[1])
                        os.setuid(term_ids[0])

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

            # PTY I/O is non-blocking and driven by the event loop. Reading
            # with a blocking os.read() in the default executor parked one of
            # its few threads per open terminal for as long as it stayed open;
            # once they were all taken, every asyncio.to_thread() in Venvy
            # (app start/stop, uploads, backups) waited forever.
            os.set_blocking(master_fd, False)

            async def _wait_fd(add, remove) -> None:
                fut = loop.create_future()

                def _ready() -> None:
                    remove(master_fd)
                    if not fut.done():
                        fut.set_result(None)

                add(master_fd, _ready)
                try:
                    await fut
                finally:
                    remove(master_fd)

            async def _pty_write(payload: bytes) -> None:
                view = memoryview(payload)
                while view:
                    try:
                        view = view[os.write(master_fd, view):]
                    except BlockingIOError:
                        await _wait_fd(loop.add_writer, loop.remove_writer)

            async def reader() -> None:
                import codecs
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    while not stop.is_set():
                        try:
                            data = os.read(master_fd, 4096)
                        except BlockingIOError:
                            await _wait_fd(loop.add_reader, loop.remove_reader)
                            continue
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
                            await _pty_write(data.encode("utf-8"))
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
                    # Reaped: the number may be handed to an unrelated process
                    # now, so the fallback kill below must not use it.
                    pid = -1

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
                # Only reached when setup failed before the normal teardown.
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.waitpid, pid, 0)
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
            "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
            "https://cdn.jsdelivr.net; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https://avatars.githubusercontent.com; "
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
# Failures are also counted per username, whatever address they come from —
# the only limit that holds against a client able to vary its apparent IP
# (see PROXY_SECRET). Higher than the per-IP limit so that ordinary typos
# from one place trip that one first.
_LOGIN_MAX_TRIES_PER_USER = 20
_LOGIN_MAX_TRACKED = 50_000

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

def _login_user_key(username: str) -> str:
    return "user:" + username.strip().lower()[:128]


def _record_login_failure(key: str, max_tries: int = _LOGIN_MAX_TRIES) -> None:
    with _login_lock:
        if key not in _login_attempts and len(_login_attempts) >= _LOGIN_MAX_TRACKED:
            return
        entry = _login_attempts.setdefault(key, {'count': 0, 'locked_until': None})
        entry['count'] += 1
        entry['last'] = time.time()
        if entry['count'] >= max_tries:
            entry['locked_until'] = time.time() + _LOGIN_LOCKOUT_SEC
            entry['count'] = 0

def _clear_login_rate(key: str) -> None:
    with _login_lock:
        _login_attempts.pop(key, None)


def _purge_login_attempts() -> None:
    """Drop entries that no longer matter (called periodically). A failure
    count is only forgotten once it has been quiet for a full lockout period:
    this used to drop every not-yet-locked entry on each sweep, which reset
    the count every two minutes and let a client guess indefinitely at four
    tries per sweep without ever being locked out."""
    now = time.time()
    with _login_lock:
        for key, entry in list(_login_attempts.items()):
            locked_until = entry.get('locked_until')
            if locked_until:
                expired = now > locked_until + _LOGIN_LOCKOUT_SEC
            else:
                expired = now > entry.get('last', 0) + _LOGIN_LOCKOUT_SEC
            if expired:
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
    user = _authenticate(session, request.cookies.get("session"), request.headers.get("x-venvy-key"))
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
    auth_key = _new_session(session, response, user)
    audit_log(user.username, "user_setup", "initial admin account created")
    return {"ok": True, "username": user.username, "role": user.role, "auth_key": auth_key}


@auth_router.post("/login")
def login(
    payload: UserLoginReq,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    rate_key = _login_rate_key(request)
    user_key = _login_user_key(payload.username)
    _check_login_rate(rate_key)
    _check_login_rate(user_key)

    def _fail(status: int, detail: str):
        _record_login_failure(rate_key)
        _record_login_failure(user_key, _LOGIN_MAX_TRIES_PER_USER)
        _check_login_rate(rate_key)   # answers 429 if that was the last allowed try
        _check_login_rate(user_key)
        raise HTTPException(status, detail)

    user = session.exec(select(User).where(User.username == payload.username)).first()
    password_ok = verify_password(payload.password, user.password_hash if user else _DUMMY_PASSWORD_HASH)
    if not user or not password_ok:
        _fail(401, "Invalid credentials.")

    if user.totp_enabled and user.totp_secret:
        if not payload.totp_code:
            raise HTTPException(403, "totp_required")
        step = _totp_verify(_decrypt_secret(user.totp_secret), payload.totp_code, after=user.totp_last_counter)
        if step is None:
            _fail(401, "Invalid authenticator code.")
        user.totp_last_counter = step
        session.add(user)   # committed with the new session below

    _clear_login_rate(user_key)
    _clear_login_rate(rate_key)
    auth_key = _new_session(session, response, user)
    audit_log(user.username, "login", f"from {rate_key}")
    return {"ok": True, "username": user.username, "role": user.role, "auth_key": auth_key}


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    token = request.cookies.get("session")
    if token:
        db_session = session.get(UserSession, _hash_token(token))
        if db_session:
            session.delete(db_session)
            session.commit()
    response.delete_cookie("session", path="/")
    return {"ok": True}


@auth_router.put("/password")
def change_own_password(
    req: PwdReq,
    request: Request,
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
    # A password change is usually "someone may know the old one" — sign out
    # every other session of this user, keep only the one making the change.
    current = _hash_token(request.cookies.get("session") or "")
    for row in session.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
        if row.token != current:
            session.delete(row)
    session.commit()
    audit_log(user.username, "password_change", "other sessions revoked")
    return {"ok": True}


@auth_router.post("/download-ticket")
def create_download_ticket(user: User = Depends(get_current_user)):
    """Trade the X-Venvy-Key header for a one-time, 60-second ticket usable as
    ?ticket= on a download URL (see get_download_user)."""
    ticket = secrets.token_urlsafe(24)
    with _download_tickets_lock:
        _download_tickets[_hash_token(ticket)] = (user.id, time.monotonic() + _DOWNLOAD_TICKET_TTL)
    return {"ticket": ticket}


# ---------------------------------------------------------------------------
# TOTP routes
# ---------------------------------------------------------------------------

@auth_router.get("/totp/setup")
def totp_setup_begin(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Generate (or return pending) TOTP secret + QR URI. Does NOT enable TOTP yet."""
    if user.totp_enabled:
        # Replacing an active authenticator must go through /totp/disable,
        # which asks for the password — a borrowed session alone must not be
        # able to re-enrol 2FA onto someone else's phone.
        raise HTTPException(400, "Two-factor authentication is already enabled. Disable it first to set up a new authenticator.")
    secret = _totp_generate_secret()
    key = f"totp_pending_{user.id}"
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = _encrypt_secret(secret)
    else:
        setting = SystemSetting(key=key, value=_encrypt_secret(secret))
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
    secret = _decrypt_secret(setting.value)
    step = _totp_verify(secret, req.totp_code)
    if step is None:
        raise HTTPException(400, "Incorrect code — try again.")
    user.totp_secret = _encrypt_secret(secret)
    user.totp_enabled = True
    user.totp_last_counter = step
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
    user.totp_last_counter = None
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


def _create_full_backup_archive(protect: Optional[Path] = None) -> Path:
    """Build a complete disaster-recovery backup — a consistent data.db
    snapshot (via SQLite's VACUUM INTO, safe on a live WAL-mode database),
    the .venvy_key encryption key, and the entire apps/ directory (code,
    venvs, logs) — bundled into one tar.gz in BACKUP_DIR. This is the only
    format the UI creates going forward: one file covers everything needed
    to restore Venvy, so backing up (and restoring) never requires touching
    the server's filesystem directly.

    Raises HTTPException on failure. Returns the path to the finished
    archive on success. Blocking (tars the whole apps/ dir) — callers on
    the event loop should run this via asyncio.to_thread; sync route
    handlers already run off the event loop via Starlette's threadpool.
    """
    BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
    # Rough space check: gzip typically halves this; refuse clearly instead
    # of filling the disk halfway through.
    raw = DB_FILE.stat().st_size if DB_FILE.exists() else 0
    for root, _dirs, files in os.walk(APPS_BASE_DIR):
        for n in files:
            with contextlib.suppress(OSError):
                raw += os.lstat(os.path.join(root, n)).st_size
    free = shutil.disk_usage(BACKUP_DIR).free
    if raw // 2 + 256 * 1024 * 1024 > free:
        raise HTTPException(507, f"Not enough free disk space for a backup (≈{raw // 2 // (1024 * 1024)} MB needed, {free // (1024 * 1024)} MB free).")
    timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
    dest = BACKUP_DIR / f"full-{timestamp}.tar.gz"

    # Same "write under a name only this request knows, then hard-link into
    # place" trick the old DB-only backup used — a concurrent request that
    # loses the race on this second-resolution timestamp can only ever
    # delete its OWN temp files, never a different request's completed one.
    suffix = secrets.token_hex(4)
    tmp_db = BACKUP_DIR / f".tmp-{timestamp}-{suffix}.db"
    tmp_archive = BACKUP_DIR / f".tmp-{timestamp}-{suffix}.tar.gz"

    try:
        conn = sqlite3.connect(str(DB_FILE))
        try:
            conn.execute("VACUUM INTO ?", (str(tmp_db),))
        finally:
            conn.close()

        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(tmp_db, arcname="data.db")
            if _VENVY_KEY_FILE.exists():
                tar.add(_VENVY_KEY_FILE, arcname=_VENVY_KEY_FILE.name)
            if APPS_BASE_DIR.exists():
                tar.add(APPS_BASE_DIR, arcname="apps")
    except Exception as e:
        logging.error(f"Full backup failed: {e}")
        tmp_archive.unlink(missing_ok=True)
        raise HTTPException(500, f"Backup failed: {e}")
    finally:
        tmp_db.unlink(missing_ok=True)

    try:
        os.link(str(tmp_archive), str(dest))
    except FileExistsError:
        raise HTTPException(409, "A backup for this exact timestamp already exists — try again.")
    finally:
        tmp_archive.unlink(missing_ok=True)
    dest.chmod(0o600)

    # Retention: keep the newest _BACKUP_KEEP full backups.
    fulls = sorted(BACKUP_DIR.glob("full-*.tar.gz"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in fulls[_BACKUP_KEEP:]:
        if protect is not None and old.name == protect.name:
            continue  # e.g. the backup a restore is about to read
        with contextlib.suppress(OSError):
            old.unlink()
            logging.info(f"Pruned old backup {old.name}")
    return dest


_BACKUP_KEEP = max(1, int(os.getenv("VENVY_BACKUP_KEEP", "10")))


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


# (pattern, type label) — "database" is a legacy DB-only snapshot from an
# older Venvy version (download/delete only, not restorable from the UI);
# "full" is the current format, listed so upgrading never orphans old backups.
_BACKUP_TYPES = (
    ("data-*.db", "database"),
    ("full-*.tar.gz", "full"),
)


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


_BACKUP_FILENAME_RE = re.compile(r"\A(?:data-\d{8}-\d{6}\.db|full-\d{8}-\d{6}\.tar\.gz)\Z")
_FULL_BACKUP_FILENAME_RE = re.compile(r"\Afull-\d{8}-\d{6}\.tar\.gz\Z")


def _resolve_backup_path(filename: str) -> Path:
    """Validate a backup filename against the fixed naming schemes used by
    _create_full_backup_archive() (current) and the retired DB-only
    create_backup() (legacy), resolve it inside BACKUP_DIR, and confirm the
    resolved path is actually still inside BACKUP_DIR (matches the
    is_safe_path pattern used for app file access elsewhere in this file) —
    rejects anything that isn't an exact, contained match."""
    if not _BACKUP_FILENAME_RE.match(filename):
        raise HTTPException(400, "Invalid backup filename.")
    path = BACKUP_DIR / filename
    if not is_safe_path(BACKUP_DIR, path) or not path.is_file():
        raise HTTPException(404, "Backup not found.")
    return path


@api_router.get("/system/backups/{filename}/download")
def download_backup(filename: str, admin: User = Depends(get_download_user)):
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    """Download a previously created backup."""
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


def _restore_tar_filter(member, dest_path):
    """tarfile's "data" filter, except for symlinks: every venv links
    bin/python3 to the system interpreter (and other names to that link),
    which "data" rejects as pointing outside the archive — so restoring any
    Python app used to fail. A symlink by itself writes nothing; the "tar"
    filter still refuses any later member whose real path (following links
    extracted earlier) would land outside the staging directory."""
    if member.issym():
        return tarfile.tar_filter(member, dest_path)
    if not (member.isreg() or member.isdir() or member.islnk()):
        # FIFOs, sockets, devices. "data" doesn't skip these, it aborts the
        # whole extraction — so one named pipe in one app's directory made
        # every backup taken since unrestorable. Nothing in them to restore.
        logging.warning(f"Restore: skipping special file {member.name}")
        return None
    return tarfile.data_filter(member, dest_path)


def _self_restart():
    """Replace this process's image with a fresh invocation of itself
    (same PID, same argv/cwd/env) so every piece of in-memory state — the
    DB engine's connections, the encryption key cache, active-process
    tracking, background tasks — reinitializes cleanly against whatever is
    now on disk. Used after a restore, where swapping data.db/apps/the key
    out from under a live process is otherwise fragile."""
    logging.warning("Restarting Venvy to complete the restore...")
    # A frozen build is its own executable: argv[0] is already the binary, and
    # prepending it again would hand the path to argparse as a stray argument,
    # so the restarted process exited instead of coming back up.
    argv = sys.argv if getattr(sys, "frozen", False) else [sys.executable] + sys.argv
    os.execv(sys.executable, argv)


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
    global _restore_in_progress
    if req.confirm != "RESTORE":
        raise HTTPException(400, 'Confirmation phrase did not match — send {"confirm": "RESTORE"}.')
    if _restore_in_progress:
        raise HTTPException(409, "A restore is already in progress.")
    if not _FULL_BACKUP_FILENAME_RE.match(filename):
        raise HTTPException(400, "Only full backups can be restored — legacy database-only backups are download-only.")
    archive_path = BACKUP_DIR / filename
    if not is_safe_path(BACKUP_DIR, archive_path) or not archive_path.is_file():
        raise HTTPException(404, "Backup not found.")

    audit_log(admin.username, "system_restore_started", f"path={archive_path}")
    session.close()
    _restore_in_progress = True
    return _start_job(admin, "restore", lambda: _restore_job(admin, archive_path))


async def _restore_job(admin: User, archive_path: Path) -> dict:
    global _restore_in_progress
    try:
        return await _run_restore(admin, archive_path)
    except BaseException:
        # Failed: this process carries on, so normal service resumes. On
        # success the flag stays set until _self_restart() replaces us.
        _restore_in_progress = False
        raise


async def _run_restore(admin: User, archive_path: Path) -> dict:

    # Safety net: back up the CURRENT state before touching anything, so a
    # restore that turns out to be a mistake can itself be undone. If this
    # fails, abort here — nothing has been touched yet.
    try:
        pre_restore = await asyncio.to_thread(_create_full_backup_archive, archive_path)
    except HTTPException as e:
        raise HTTPException(500, f"Could not create a safety backup before restoring — aborted, nothing was touched: {e.detail}")
    audit_log(admin.username, "system_restore_safety_backup", f"path={pre_restore}")

    staging = BACKUP_DIR / f".restore-staging-{secrets.token_hex(4)}"
    staging.mkdir()

    def _extract():
        with tarfile.open(archive_path, "r:gz") as tar:
            # Reject anything that would land outside `staging` (a crafted
            # or corrupted archive with "../" entries) before extracting a
            # single byte. tarfile's own filter="data" (PEP 706) covers this
            # on 3.12+, but this file supports older interpreters too, so
            # check explicitly regardless of Python version.
            for member in tar.getmembers():
                target = (staging / member.name).resolve()
                if not is_safe_path(staging, target):
                    raise ValueError(f"Backup archive contains an unsafe path: {member.name}")
            try:
                tar.extractall(staging, filter=_restore_tar_filter)
            except TypeError:
                tar.extractall(staging)  # Python < 3.12 has no `filter` kwarg

    def _swap():
        if (staging / "apps").is_dir():
            if APPS_BASE_DIR.exists():
                shutil.rmtree(APPS_BASE_DIR)
            shutil.move(str(staging / "apps"), str(APPS_BASE_DIR))
        # The database is loaded *into* the live file through SQLite's backup
        # API, not swapped underneath it. Requests keep arriving during a
        # restore (the UI polls this very job), and replacing data.db while
        # deleting its -wal/-shm by hand left two windows in which a
        # connection could pair the new database with the old one's WAL —
        # which SQLite would then replay onto it. The backup API takes the
        # proper locks and goes through the WAL like any other write.
        engine.dispose()
        src = sqlite3.connect(str(staging / "data.db"))
        try:
            dst = sqlite3.connect(str(DB_FILE), timeout=60)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        engine.dispose()
        restored_key = staging / _VENVY_KEY_FILE.name
        if restored_key.is_file():
            shutil.move(str(restored_key), str(_VENVY_KEY_FILE))
            _VENVY_KEY_FILE.chmod(0o600)

    try:
        # Unpack and check the archive first: if it is unusable, nothing has
        # been stopped or changed yet.
        try:
            await asyncio.to_thread(_extract)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except (tarfile.TarError, OSError, EOFError) as e:
            raise HTTPException(400, f"Backup archive could not be read — restore aborted, nothing was changed: {e}")
        if not (staging / "data.db").is_file():
            raise HTTPException(400, "Backup archive is missing data.db — restore aborted, nothing was changed.")

        # Stop every running app before touching apps/ or data.db out from under them.
        with Session(engine) as session:
            for a in session.exec(select(App)).all():
                try:
                    await stop_app_process_logic(a.id, session, manual=True)
                except Exception as e:
                    logging.warning(f"Failed to stop app {a.id} before restore: {e}")

        await asyncio.to_thread(_swap)
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)

    audit_log(admin.username, "system_restore_completed", f"path={archive_path}")

    # Venvy's in-memory state is now stale relative to what's on disk —
    # restart so everything reinitializes cleanly. Delay briefly so this
    # response actually reaches the client before the process image swaps.
    # Give the client a few polls to see "done" before the process swaps.
    asyncio.get_running_loop().call_later(3.0, _self_restart)
    return {"ok": True, "restarting": True}


# Uploads. The UI sends every file through the chunked endpoints below, so
# neither a proxy's request-body cap (Cloudflare: 100 MB) nor the multipart
# parser's per-request file-count limit applies. The single-request endpoints
# (/apps/upload with files, /files/upload) remain for API clients.
_SINGLE_REQUEST_UPLOAD_MAX = 50 * 1024 * 1024
_UPLOAD_CHUNK_MAX = 16 * 1024 * 1024
_UPLOAD_FILE_MAX = int(os.getenv("VENVY_MAX_UPLOAD_MB", "10240")) * 1024 * 1024
_UPLOAD_TMP_DIR = DATA_DIR / ".upload-tmp"
_UPLOAD_STALE_SEC = 24 * 3600
_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")
_UPLOAD_COPY_BUF = 1024 * 1024


def _save_upload_file(file: UploadFile, dest: Path, limit: int) -> bool:
    """Stream *file* to *dest* without holding it in memory. Returns False (and
    removes the partial file) if it is larger than *limit* bytes."""
    written = 0
    file.file.seek(0)
    with open(dest, "wb") as out:
        while True:
            buf = file.file.read(_UPLOAD_COPY_BUF)
            if not buf:
                break
            written += len(buf)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                return False
            out.write(buf)
    return True


def _purge_stale_upload_parts() -> None:
    cutoff = time.time() - _UPLOAD_STALE_SEC
    try:
        for part in _UPLOAD_TMP_DIR.glob("*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _upload_part_path(app_id: int, user_id: int, upload_id: str) -> Path:
    if not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(400, "Invalid upload_id.")
    return _UPLOAD_TMP_DIR / f"{app_id}-{user_id}-{upload_id}.part"


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
    if _restore_in_progress and action != "stop":
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
    try:
        parts = _app_rel_parts(root, path)
    except AppPathError as exc:
        raise HTTPException(exc.status)
    results = []
    _SKIP_NAMES = {"venv", "__pycache__", "logs", "node_modules", ".git", ".mypy_cache", ".pytest_cache"}
    for item in _fs_listdir(root, parts):
        if item["name"] in _SKIP_NAMES or _is_protected_app_file(item["name"]):
            continue
        results.append({
            "name": item["name"],
            "path": "/".join(parts + (item["name"],)),
            "is_dir": item["is_dir"],
            "size": item["size"],
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
    op = req.operation
    if op not in ("create_file", "create_folder", "delete", "rename", "copy", "save"):
        raise HTTPException(400, f"Unknown operation: {op}")

    try:
        # delete/rename/copy act on the entry itself; the others on what it names.
        parts = _app_rel_parts(root, req.path, follow_last=op not in ("delete", "rename", "copy"))
        if parts and _is_protected_app_file(parts[-1]):
            raise HTTPException(403, "Operation not permitted on this path.")
        if not parts and op != "create_folder":
            raise HTTPException(400, "That operation isn't allowed on the app's root folder.")
        owner = _app_ids(app_row)   # new files and folders go to the app's user

        if op == "create_file":
            await asyncio.to_thread(_fs_touch, root, parts, owner)

        elif op == "create_folder":
            await asyncio.to_thread(_fs_mkdir, root, parts, owner)

        elif op == "delete":
            await asyncio.to_thread(_fs_delete, root, parts)

        elif op in ("rename", "copy"):
            new_parts = _app_rel_parts(root, req.new_path or "", follow_last=False)
            if not new_parts:
                raise HTTPException(403)
            if _is_protected_app_file(new_parts[-1]):
                raise HTTPException(403, "Operation not permitted on this path.")
            await asyncio.to_thread(
                _fs_move_or_copy, root, parts, new_parts, op == "copy", owner, req.new_path or "")

        elif op == "save":
            if req.content is None:
                raise HTTPException(400, "No content provided for save.")
            await asyncio.to_thread(_fs_save, root, parts, req.content.encode("utf-8"), owner)
    except HTTPException:
        raise
    except AppPathError as exc:
        raise HTTPException(exc.status, exc.detail or None)
    except Exception as exc:
        raise HTTPException(500, f"File operation failed: {exc}")
    return {"ok": True}


@api_router.get("/apps/{app_id}/files/download")
async def download_file(
    app_id: int,
    path: str,
    session: Session = Depends(get_session),
    # See file_tree() above — same reasoning, viewers don't get file contents.
    user: User = Depends(get_download_user),
):
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator or admin privileges required.")
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    try:
        parts = _app_rel_parts(root, path)
        fileobj, st = await asyncio.to_thread(_fs_open_file, root, parts)
    except AppPathError as exc:
        raise HTTPException(exc.status, exc.detail or None)
    return _file_download_response(fileobj, st, parts[-1])


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
    owner = _app_ids(app_row)
    skipped: List[Dict[str, str]] = []
    for file in files:
        rel_path = file.filename or "upload"
        reason = _upload_rel_path_error(rel_path)
        parts: tuple = ()
        if not reason:
            try:
                parts = _app_rel_parts(root, str(target_dir / rel_path))
            except AppPathError:
                reason = "path escapes app directory"
        if not reason:
            file.file.seek(0)
            try:
                if not await asyncio.to_thread(_fs_write_stream, root, parts, file.file, _SINGLE_REQUEST_UPLOAD_MAX, owner):
                    reason = f"exceeds the {_SINGLE_REQUEST_UPLOAD_MAX // (1024 * 1024)} MB single-request limit"
            except AppPathError as exc:
                reason = exc.detail or "invalid path"
        if reason:
            skipped.append({"path": rel_path, "reason": reason})
    return {"ok": True, "skipped": skipped}


@api_router.post("/apps/{app_id}/files/upload/chunk")
async def upload_file_chunk(
    app_id: int,
    upload_id: str = Form(...),
    path: str = Form(""),
    rel_path: str = Form(...),
    offset: int = Form(...),
    total: int = Form(...),
    chunk: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    # Deliberately not rate_limit_mutations: one large file is hundreds of
    # chunk requests. Access still requires an operator session + CSRF.
):
    """Append one chunk of a file upload. The client sends a file's chunks in
    order; offset must equal the bytes received so far (a mismatch answers
    409 with the server's count, so the client can resume from there). When
    the part reaches *total* bytes it is moved into place atomically."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target_dir = (root / path).resolve()
    if not is_safe_path(root, target_dir):
        raise HTTPException(403)
    if not await asyncio.to_thread(target_dir.is_dir):
        raise HTTPException(404, "Target directory does not exist.")
    reason = _upload_rel_path_error(rel_path)
    if reason:
        raise HTTPException(400, f"'{rel_path}': {reason}.")
    try:
        dest_parts = _app_rel_parts(root, str(target_dir / rel_path))
    except AppPathError:
        raise HTTPException(403, "Upload path escapes app directory.")
    if total < 0 or offset < 0 or offset > total:
        raise HTTPException(400, "Invalid offset/total.")
    if total > _UPLOAD_FILE_MAX:
        raise HTTPException(413, f"File exceeds the {_UPLOAD_FILE_MAX // (1024 * 1024)} MB upload limit.")

    part = _upload_part_path(app_id, user.id, upload_id)

    def _append() -> int:
        _UPLOAD_TMP_DIR.mkdir(mode=0o700, exist_ok=True)
        if offset == 0:
            _purge_stale_upload_parts()
            free = shutil.disk_usage(_UPLOAD_TMP_DIR).free
            if total + 256 * 1024 * 1024 > free:
                raise HTTPException(507, "Not enough free disk space for this upload.")
            mode = "wb"
        else:
            try:
                have = part.stat().st_size
            except FileNotFoundError:
                have = 0
            if have != offset:
                raise HTTPException(409, {"message": "Offset mismatch.", "received": have})
            mode = "ab"
        written = 0
        with open(part, mode) as out:
            while True:
                buf = chunk.file.read(_UPLOAD_COPY_BUF)
                if not buf:
                    break
                written += len(buf)
                if written > _UPLOAD_CHUNK_MAX or offset + written > total:
                    out.close()
                    part.unlink(missing_ok=True)
                    raise HTTPException(400, "Chunk is larger than allowed.")
                out.write(buf)
        return offset + written

    received = await asyncio.to_thread(_append)
    if received < total:
        return {"ok": True, "received": received, "done": False}

    def _finalize() -> None:
        try:
            _fs_install(root, dest_parts, part, _app_ids(app_row))
        except AppPathError as exc:
            part.unlink(missing_ok=True)
            raise HTTPException(exc.status, exc.detail or f"'{rel_path}' cannot be written.")

    await asyncio.to_thread(_finalize)
    return {"ok": True, "received": received, "done": True}


@api_router.delete("/apps/{app_id}/files/upload/{upload_id}")
async def abort_file_upload(
    app_id: int,
    upload_id: str,
    user: User = Depends(require_operator),
):
    """Discard a partially uploaded file (e.g. after the user cancels)."""
    part = _upload_part_path(app_id, user.id, upload_id)
    await asyncio.to_thread(part.unlink, missing_ok=True)
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


def _sanitize_cf_value(v: str) -> str:
    """Strip ALL whitespace and invisible/zero-width Unicode chars."""
    v = v.strip()
    v = "".join(ch for ch in v if ch not in "\r\n\t")
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")
    return v


def _is_secret_setting(key: str) -> bool:
    """Settings whose value is never returned by the API once stored."""
    return key in ("cf_api_token", "gh_token") or key.startswith("totp_pending_")


def _reject_reserved_setting(key: str) -> None:
    """Keys Venvy keeps its own state under are not settable through the API."""
    if key.startswith("totp_pending_"):
        raise HTTPException(400, f"'{key}' is reserved.")


def _setting_for_storage(key: str, value: str) -> tuple:
    """(cleaned value, value as stored) for a system setting — the one place
    that decides which settings are encrypted at rest."""
    if key.startswith("cf_"):
        value = _sanitize_cf_value(value)
        return value, _encrypt_secret(value)
    if key == "gh_token":
        return value, _encrypt_secret(value)
    return value, value


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


# ---------------------------------------------------------------------------
# GitHub integration — device-flow sign-in + per-app git operations
# ---------------------------------------------------------------------------
# Sign-in uses GitHub's OAuth Device Flow: the admin registers a free GitHub
# OAuth App (Client ID only, no secret, no hosted callback) once, then
# "Connect GitHub" shows a short code and opens github.com/login/device in
# the user's own browser tab. We poll GitHub until they approve it there.

_GH_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GH_API_BASE = "https://api.github.com"

# Pending device-flow authorizations: flow_id -> {device_code, client_id, interval, expires_at}
_gh_device_flows: Dict[str, dict] = {}


def _gh_client_id(session: Session) -> str:
    row = session.get(SystemSetting, "gh_client_id")
    return (row.value if row else "").strip()


def _gh_token(session: Session) -> str:
    row = session.get(SystemSetting, "gh_token")
    return _decrypt_secret(row.value) if row and row.value else ""


def _gh_setting(session: Session, key: str) -> str:
    row = session.get(SystemSetting, key)
    return row.value if row else ""


def _gh_save(session: Session, key: str, value: str) -> None:
    stored = _encrypt_secret(value) if key == "gh_token" else value
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = stored
    else:
        setting = SystemSetting(key=key, value=stored)
    session.add(setting)


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


def _git_auth_config(token: str) -> List[str]:
    """-c args that inject the GitHub token as a Basic-auth header scoped to
    github.com only — never touches the stored remote URL or argv."""
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


async def _run_git(app_row: "App", args: List[str], timeout: int = 60, token: str = "") -> str:
    """Run a git command in the app's directory as the app's user; return stdout,
    raise HTTPException with (token-redacted) stderr on failure."""
    env = _app_tool_env(app_row)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "safe.directory=*", *args,
            cwd=app_row.app_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **_app_proc_kwargs(app_row),
        )
    except FileNotFoundError:
        raise HTTPException(500, "git is not installed on the server.")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, "git command timed out.")
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "git command failed."
        if token:
            msg = msg.replace(token, "***")
        raise HTTPException(400, msg)
    return stdout.decode(errors="replace")


async def _git_status_payload(app_row: "App", session: Session) -> dict:
    app_dir = Path(app_row.app_dir)
    if not await asyncio.to_thread((app_dir / ".git").exists):
        return {"is_repo": False}
    branch = (await _run_git(app_row, ["branch", "--show-current"], timeout=15)).strip()
    try:
        remote_url = (await _run_git(app_row, ["remote", "get-url", "origin"], timeout=15)).strip()
    except HTTPException:
        remote_url = ""
    dirty_out = await _run_git(app_row, ["status", "--porcelain"], timeout=20)
    dirty_count = len([l for l in dirty_out.splitlines() if l.strip()])
    ahead = behind = 0
    if branch:
        try:
            counts = (await _run_git(
                app_row, ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"], timeout=15,
            )).strip()
            parts = counts.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
        except HTTPException:
            pass
    try:
        last_commit = (await _run_git(app_row, ["log", "-1", "--format=%h %s (%cr)"], timeout=15)).strip()
    except HTTPException:
        last_commit = ""
    return {
        "is_repo": True,
        "branch": branch,
        "remote_url": remote_url,
        "dirty_count": dirty_count,
        "ahead": ahead,
        "behind": behind,
        "last_commit": last_commit,
    }


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
    user: User = Depends(require_operator),
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

    try:
        await terminal_manager.start_terminal(
            websocket, app_id, ws_user_id, ws_user_role
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