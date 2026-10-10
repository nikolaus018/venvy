"""Paths, environment-driven settings and logging setup.

Everything here is fixed at import time. Venvy's persistent state lives
under DATA_DIR — the directory the server is started from.
"""
import datetime
import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Set

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None

try:
    import tomllib as _toml
except ImportError:
    # Fallback for Python < 3.11 (though Venvy recommends 3.11+)
    _toml = None

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Where the shipped, read-only files live: the repository root (or the
# unpacked bundle of a frozen build). The UI is under web/, third-party
# scripts under vendor/.
if getattr(sys, 'frozen', False):
    ASSETS_DIR = Path(sys._MEIPASS)
else:
    ASSETS_DIR = Path(__file__).parent.parent.resolve()

WEB_DIR = ASSETS_DIR / "web"

DATA_DIR = Path.cwd()
APPS_BASE_DIR = DATA_DIR / "apps"
BACKUP_DIR = DATA_DIR / "backups"
DB_FILE = DATA_DIR / "data.db"
DATABASE_URL = f"sqlite:///{DB_FILE}"
TERMINAL_RC_DIR = DATA_DIR / ".terminal"
APP_LOGS_DIR = DATA_DIR / "app-logs"
_UPLOAD_TMP_DIR = DATA_DIR / ".upload-tmp"
_VENVY_KEY_FILE = DATA_DIR / ".venvy_key"

SESSION_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — keeps PWA users signed in

# Default False so plain-HTTP LAN deployments work out of the box.
# Set SESSION_COOKIE_SECURE=1 in env when running behind HTTPS.
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

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
# Venvy's own settings, which the processes it starts must not inherit. The
# proxy secret is the obvious one, but HOST and PORT matter just as much:
# the systemd unit sets them for Venvy, and a hosted app that reads $PORT
# would otherwise be told to bind Venvy's own port.
_PRIVATE_ENV_VARS = (
    "HOST", "PORT", "LOG_LEVEL", "SESSION_COOKIE_SECURE", "ALLOWED_ORIGINS",
    "IP_ALLOWLIST", "TRUSTED_PROXY_IPS",
)
_PRIVATE_ENV_PREFIX = "VENVY_"


def child_base_env() -> dict:
    """os.environ for a process started on an app's behalf, minus Venvy's own
    configuration."""
    return {
        k: v for k, v in os.environ.items()
        if k not in _PRIVATE_ENV_VARS and not k.startswith(_PRIVATE_ENV_PREFIX)
    }

if IP_ALLOWLIST is not None and not PROXY_SECRET and TRUSTED_PROXY_IPS & {"127.0.0.1", "::1"}:
    logging.warning(
        "IP_ALLOWLIST is set, but CF-Connecting-IP is trusted from loopback without "
        "VENVY_PROXY_SECRET: software running on this host (including hosted apps) "
        "can claim any client IP. Set VENVY_PROXY_SECRET to close that.")

_ALLOWED_ORIGINS_RAW = os.getenv("ALLOWED_ORIGINS", "").strip()
_ALLOWED_ORIGINS: List[str] = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]

if not _ALLOWED_ORIGINS:
    logging.warning(
        "ALLOWED_ORIGINS is not set. Defaulting to empty origins (Same-origin only). "
        "For external access, set ALLOWED_ORIGINS='https://yourdomain.com' for security."
    )


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _make_tz_aware(dt: datetime.datetime) -> datetime.datetime:
    """Return *dt* with UTC tzinfo attached if it is naive."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


# Tracks exactly when this Venvy instance started up.
_VENVY_BOOT_TIME = utc_now()
