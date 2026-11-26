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
    keep_releases: Optional[int] = Field(default=None)     # releases kept per app; None = the server default
    last_deploy_at: Optional[datetime.datetime] = Field(default=None)
    last_deploy_status: Optional[str] = Field(default=None)  # ok | failed | rolled-back | running
    last_deploy_commit: Optional[str] = Field(default=None)

    # Own hostname: requests to Venvy with this Host header go to the app
    domain: Optional[str] = Field(default=None, index=True)
    runtime_version: Optional[str] = Field(default=None)    # "3.12" (Python), "20" (Node), "1.82" (Rust) ...
    disk_limit_mb: Optional[int] = Field(default=None)
    disk_limit_action: str = Field(default="alert")         # alert | stop

    # How the app is run (see appspec.py). All optional: blank means the
    # runtime's own default.
    start_command: Optional[str] = Field(default=None)      # argv, shell-style quoting, $VARS expanded
    install_command: Optional[str] = Field(default=None)    # replaces the manifest install
    release_command: Optional[str] = Field(default=None)    # after the build, before the restart (migrations)
    root_dir: str = Field(default="")                       # the app's folder inside app_dir (monorepos)
    stop_signal: str = Field(default="SIGTERM")
    stop_timeout: Optional[int] = Field(default=None)       # seconds; None = the server default
    container_port: Optional[int] = Field(default=None)     # docker: the port inside the container

    # Health check: http (healthcheck_path), tcp (the port accepts), command (exit 0)
    health_type: str = Field(default="http")
    health_command: Optional[str] = Field(default=None)
    health_start_period: int = Field(default=0)             # seconds after a start in which failures don't count

    # Processes: an app may have workers, replicas and companions that share
    # its folder, user and settings. They are App rows of their own with
    # parent_app_id set; process_kind says which.
    parent_app_id: Optional[int] = Field(default=None, index=True)
    process_kind: str = Field(default="app")                # app | worker | replica | tunnel
    process_name: Optional[str] = Field(default=None)       # "worker", "scheduler" ... (Procfile names)
    instances: int = Field(default=1)                       # web processes behind the proxy
    zero_downtime: bool = Field(default=False)              # restart by starting the new one first

    # Deploys
    deploy_strategy: str = Field(default="in_place")        # in_place | atomic
    spec_from_repo: bool = Field(default=True)              # apply venvy.toml / Procfile on deploy
    commit_status: bool = Field(default=False)              # report deploys to GitHub / GitLab / Gitea
    previews_enabled: bool = Field(default=False)           # a preview app per pushed branch
    preview_domain: Optional[str] = Field(default=None)     # previews get <branch>.<preview_domain>
    preview_env: str = Field(default="")                    # encrypted JSON, overrides for previews
    preview_of: Optional[int] = Field(default=None, index=True)
    environment: str = Field(default="production")          # a label: production, staging, preview ...

    # Network and sandbox
    bind_public: bool = Field(default=True)                 # False: only reachable through Venvy (firewalled)
    egress_policy: str = Field(default="allow")             # allow | deny | allowlist
    egress_allow: str = Field(default="")                   # comma separated IPs / CIDRs for allowlist
    sandbox: bool = Field(default=True)                     # filesystem sandbox when run as a systemd service
    pids_limit: Optional[int] = Field(default=None)
    io_weight: Optional[int] = Field(default=None)          # 1-10000, systemd IOWeight
    nofile_limit: Optional[int] = Field(default=None)
    access_log: bool = Field(default=True)                  # log requests that reach it through the proxy

    # Secrets beyond environment variables
    secret_files: str = Field(default="")                   # encrypted JSON {name: content}
    sops_file: Optional[str] = Field(default=None)          # repo file decrypted with SOPS at start
    vault_path: Optional[str] = Field(default=None)         # Vault KV v2 path merged into the environment

    # A managed service (postgres, redis ...) is an app made from a template.
    service_template: Optional[str] = Field(default=None)


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


class Route(SQLModel, table=True):
    """A hostname (and path) Venvy's proxy serves for an app, or redirects."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    hostname: str = Field(index=True)               # app.example.com or *.example.com
    path_prefix: str = Field(default="/")
    strip_prefix: bool = Field(default=False)
    kind: str = Field(default="proxy")              # proxy | redirect
    redirect_to: Optional[str] = Field(default=None)    # host or URL a redirect goes to
    redirect_code: int = Field(default=301)
    basic_auth: str = Field(default="")             # encrypted JSON {user: scrypt hash}
    ip_allow: str = Field(default="")               # comma separated IPs / CIDRs; blank = everyone
    rate_limit: int = Field(default=0)              # requests per minute per client IP; 0 = none
    max_body_mb: int = Field(default=0)             # 0 = no limit
    headers: str = Field(default="{}")              # JSON {name: value} added to responses
    gzip: bool = Field(default=False)
    cache_seconds: int = Field(default=0)           # cache public GET responses up to this long
    tls: bool = Field(default=True)                 # ask the TLS front (Caddy) for a certificate
    created_at: datetime.datetime = Field(default_factory=utc_now)


class PortForward(SQLModel, table=True):
    """A raw TCP or UDP port on the host that is passed to an app's port."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    protocol: str = Field(default="tcp")            # tcp | udp
    listen_host: str = Field(default="0.0.0.0")
    listen_port: int = Field(unique=True)
    target_port: int
    enabled: bool = Field(default=True)


class Volume(SQLModel, table=True):
    """A folder several apps can share."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    created_at: datetime.datetime = Field(default_factory=utc_now)


class AppVolume(SQLModel, table=True):
    app_id: int = Field(primary_key=True)
    volume_id: int = Field(primary_key=True)
    read_only: bool = Field(default=False)


class ServiceBinding(SQLModel, table=True):
    """An app that uses a managed service: its connection variables are
    added to the app's environment at start."""
    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    service_app_id: int = Field(index=True)
    prefix: str = Field(default="")                 # "" -> DATABASE_URL; "CACHE" -> CACHE_URL


class UrlMonitor(SQLModel, table=True):
    """A URL or TCP endpoint Venvy checks on a schedule (anything, not only its apps)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    target: str                                     # https://... or tcp://host:port
    interval: int = Field(default=60)
    timeout: int = Field(default=10)
    expect_status: int = Field(default=0)           # 0 = any answer below 400
    enabled: bool = Field(default=True)
    public: bool = Field(default=False)             # shown on the status page
    last_ok: Optional[bool] = Field(default=None)
    last_checked: Optional[datetime.datetime] = Field(default=None)
    last_error: str = Field(default="")
    last_ms: Optional[float] = Field(default=None)


class UptimeSample(SQLModel, table=True):
    """One check result: an app's health ("app:<id>") or a monitor ("mon:<id>")."""
    id: Optional[int] = Field(default=None, primary_key=True)
    target: str = Field(index=True)
    ts: datetime.datetime = Field(default_factory=utc_now, index=True)
    ok: bool = Field(default=True)
    ms: Optional[float] = Field(default=None)


class RemoteServer(SQLModel, table=True):
    """Another Venvy this one manages, through an API token on it."""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    url: str
    token: str                                      # encrypted
    created_at: datetime.datetime = Field(default_factory=utc_now)
    last_status: str = Field(default="")
    last_seen: Optional[datetime.datetime] = Field(default=None)
