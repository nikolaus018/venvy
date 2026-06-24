import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Set, Union

from fastapi import HTTPException
from starlette.requests import HTTPConnection, Request

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

if getattr(sys, "frozen", False):
    ASSETS_DIR = Path(sys._MEIPASS)
else:
    ASSETS_DIR = Path(__file__).parent.resolve()

DATA_DIR = Path.cwd()
APPS_BASE_DIR = DATA_DIR / "apps"
DB_FILE = DATA_DIR / "data.db"
DATABASE_URL = f"sqlite:///{DB_FILE}"

SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — keeps PWA users signed in

# Default False so plain-HTTP LAN deployments work out of the box.
# Set SESSION_COOKIE_SECURE=1 in env when running behind HTTPS.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

# ---------------------------------------------------------------------------
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
IP_ALLOWLIST: Optional[Set[str]] = {
    ip.strip() for ip in IP_ALLOWLIST_RAW.split(",") if ip.strip()
} or None


def _resolve_client_ip(request: Union[Request, HTTPConnection]) -> str:
    """Helper to resolve the real client IP, considering X-Forwarded-For."""
    ip = ""
    # Prefer X-Forwarded-For set by trusted proxy (e.g. cloudflared)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        ip = xff.split(",")[0].strip()
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
        raise HTTPException(
            status_code=403, detail="Access from this IP is not allowed."
        )
