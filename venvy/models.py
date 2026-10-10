"""Database tables."""
import datetime
from typing import Optional

from sqlmodel import Field, SQLModel

from venvy.config import utc_now


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
