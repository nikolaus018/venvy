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
    # False: the user only sees the apps listed in AppAccess (admins always see all).
    all_apps: bool = Field(default=True)


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
    # Nullable = unlimited, which is what a new app starts with.
    memory_limit_mb: Optional[int] = Field(default=None)
    cpu_limit_percent: Optional[int] = Field(default=None)
    # Set when the app is launched under a systemd scope, so stop/kill logic knows
    # to use `systemctl stop` instead of (or in addition to) signal-based teardown.
    systemd_scope: Optional[str] = Field(default=None)
    # Start-time fingerprint of `pid` (see _proc_start_id). A PID alone can be
    # reused by an unrelated process after a crash or reboot; this lets Venvy
    # tell "our app is still running" from "something else got that number".
    pid_start: Optional[str] = Field(default=None)

    # Health check: Venvy requests this path on the app's port while it runs.
    health_interval: int = Field(default=30)        # seconds between probes
    health_failures: int = Field(default=3)         # consecutive failures before "unhealthy"
    health_restart: bool = Field(default=True)      # restart an app that stays unhealthy

    # Deploying from git
    git_url: Optional[str] = Field(default=None)
    git_branch: str = Field(default="main")
    git_auth: Optional[str] = Field(default=None)           # encrypted JSON: {"token": ...}
    build_command: Optional[str] = Field(default=None)      # run after every deploy, before the restart
    webhook_secret: Optional[str] = Field(default=None)     # encrypted; signs push webhooks
    auto_deploy: bool = Field(default=False)                # a verified push webhook deploys
    last_deploy_at: Optional[datetime.datetime] = Field(default=None)
    last_deploy_status: Optional[str] = Field(default=None)  # ok | failed | rolled-back | running
    last_deploy_commit: Optional[str] = Field(default=None)

    # Own hostname: requests to Venvy with this Host header go to the app
    domain: Optional[str] = Field(default=None, index=True)
    runtime_version: Optional[str] = Field(default=None)    # e.g. "3.12" for Python
    disk_limit_mb: Optional[int] = Field(default=None)      # soft: alerts when exceeded


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


class AuditEntry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime.datetime = Field(default_factory=utc_now, index=True)
    username: str = Field(default="", index=True)
    action: str = Field(default="", index=True)
    detail: str = Field(default="")
    ip: str = Field(default="")


class ApiToken(SQLModel, table=True):
    """A long-lived credential for scripts and CI (Authorization: Bearer ...).
    Only the SHA-256 of the token is stored."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str
    token_hash: str = Field(unique=True, index=True)
    prefix: str = Field(default="")                 # first characters, to recognise it in a list
    scope: str = Field(default="full")              # full | read
    created_at: datetime.datetime = Field(default_factory=utc_now)
    expires_at: Optional[datetime.datetime] = Field(default=None)
    last_used_at: Optional[datetime.datetime] = Field(default=None)


class AppAccess(SQLModel, table=True):
    """Which apps a user with all_apps=False may see and use."""
    user_id: int = Field(primary_key=True)
    app_id: int = Field(primary_key=True)


class NotifyChannel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    kind: str                                       # webhook | slack | discord | ntfy | telegram | email
    config: str = Field(default="")                 # encrypted JSON
    events: str = Field(default="*")                # comma separated event names, or *
    enabled: bool = Field(default=True)
    last_status: Optional[str] = Field(default=None)
    last_sent_at: Optional[datetime.datetime] = Field(default=None)


class MetricSample(SQLModel, table=True):
    """One minute of CPU/memory, per app (app_id) or for the whole host (app_id 0)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    ts: datetime.datetime = Field(index=True)
    cpu: float = Field(default=0.0)                 # percent (apps: of one core)
    mem: float = Field(default=0.0)                 # bytes (apps) / percent (host)
    disk: float = Field(default=0.0)                # bytes (apps) / percent (host); 0 = not measured


class AlertRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(default=0)                  # 0 = the host
    metric: str                                     # cpu | memory | disk
    threshold: float                                # percent (host), percent of one core (app cpu), MB (app memory/disk)
    minutes: int = Field(default=5)                 # for this long before it fires
    enabled: bool = Field(default=True)
    firing: bool = Field(default=False)
    since: Optional[datetime.datetime] = Field(default=None)


class ScheduledJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    name: str
    schedule: str                                   # 5-field cron expression
    command: str
    timeout: int = Field(default=600)
    enabled: bool = Field(default=True)
    last_run_at: Optional[datetime.datetime] = Field(default=None)
    last_status: Optional[str] = Field(default=None)    # ok | failed | timeout | running
    last_exit_code: Optional[int] = Field(default=None)
    last_duration: Optional[float] = Field(default=None)
    last_output: str = Field(default="")


class Release(SQLModel, table=True):
    """A point an app can be rolled back to: a git commit, or a snapshot of its files."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now)
    kind: str = Field(default="deploy")             # deploy | before-deploy | manual
    commit: Optional[str] = Field(default=None)
    message: str = Field(default="")
    archive: Optional[str] = Field(default=None)    # file name in the releases directory (non-git apps)
    size: int = Field(default=0)
    by: str = Field(default="")


class EnvHistory(SQLModel, table=True):
    """What changed in an app's environment variables, and when. Names only —
    the values are secrets and are never copied here."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    ts: datetime.datetime = Field(default_factory=utc_now)
    username: str = Field(default="")
    added: str = Field(default="")                  # comma separated names
    removed: str = Field(default="")
    changed: str = Field(default="")
