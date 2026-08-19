"""SQLModel table definitions.

Timestamps are stored in UTC. Secrets (``App.env_json`` and the ``cf_*``
system settings) are stored encrypted — see :mod:`venvy.core.crypto` — so
reading them requires going through the serializer rather than the column.
"""

from __future__ import annotations

import datetime

from sqlmodel import Field, SQLModel

from venvy.core.timeutils import utc_now

__all__ = [
    "VALID_RESTART_POLICIES",
    "VALID_ROLES",
    "VALID_RUNTIMES",
    "VALID_STATUSES",
    "App",
    "CfRoute",
    "SystemSetting",
    "User",
    "UserSession",
]

#: Roles, from most to least privileged.
#:   admin    — manage users and system settings, plus everything an operator can do
#:   operator — full app lifecycle: create, edit, start/stop, files, packages, tunnels
#:   viewer   — read-only: app status, logs and metrics
VALID_ROLES = ("admin", "operator", "viewer")

#: Application runtimes Venvy knows how to build and launch.
VALID_RUNTIMES = ("python", "rust")

#: What the supervisor should do when an app exits.
VALID_RESTART_POLICIES = ("never", "on-failure", "always")

#: Lifecycle states. "starting" and "stopping" are transient markers set by the
#: API so the UI can show progress while the real work happens in the
#: background; the supervisor never acts on them.
VALID_STATUSES = ("stopped", "starting", "running", "stopping", "error")


class User(SQLModel, table=True):
    """An account that can sign in to Venvy."""

    id: int | None = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="operator")
    created_at: datetime.datetime = Field(default_factory=utc_now)
    #: Base32 TOTP secret, or None when two-factor auth is off.
    totp_secret: str | None = Field(default=None)
    totp_enabled: bool = Field(default=False)


class UserSession(SQLModel, table=True):
    """A signed-in browser session, keyed by the opaque cookie value."""

    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now)
    expires_at: datetime.datetime


class App(SQLModel, table=True):
    """A managed application: its code, its virtualenv and its runtime state."""

    id: int | None = Field(default=None, primary_key=True)
    #: Who created this app. An audit field only — apps belong to the instance,
    #: so every authenticated user sees every app and deleting a user never
    #: deletes their apps.
    user_id: int = Field(index=True)
    name: str = Field(index=True)
    slug: str
    runtime: str = Field(default="python")
    created_at: datetime.datetime = Field(default_factory=utc_now)
    updated_at: datetime.datetime = Field(default_factory=utc_now)

    # ── Runtime state ────────────────────────────────────────────────────────
    status: str = Field(default="stopped")
    pid: int | None = Field(default=None)

    # ── Configuration ────────────────────────────────────────────────────────
    venv_path: str
    app_dir: str
    entrypoint: str
    start_on_boot: bool = Field(default=False)
    restart_policy: str = Field(default="never")

    # ── History ──────────────────────────────────────────────────────────────
    last_exit_code: int | None = Field(default=None)
    last_start_time: datetime.datetime | None = Field(default=None)
    last_stop_time: datetime.datetime | None = Field(default=None)
    #: Persisted so that a restart of Venvy itself does not resurrect apps the
    #: operator had deliberately stopped under an "always" restart policy.
    last_manually_stopped_at: datetime.datetime | None = Field(default=None)

    # ── Network and environment ──────────────────────────────────────────────
    port: int | None = Field(default=None)
    #: JSON array of ints. When non-empty these override port auto-detection.
    manual_ports: str = Field(default="[]")
    #: JSON array of ints observed by the supervisor.
    detected_ports: str = Field(default="[]")
    external_url: str | None = Field(default=None)
    #: Encrypted JSON object of environment variables passed to the app.
    env_json: str = Field(default="{}")
    healthcheck_path: str | None = Field(default=None)
    #: Which URL the UI shows as the app's address: "local" or "public".
    url_source_pref: str = Field(default="local")

    # ── Resource isolation ───────────────────────────────────────────────────
    #: Enforced through systemd-run when available. None means unlimited.
    memory_limit_mb: int | None = Field(default=512)
    cpu_limit_percent: int | None = Field(default=50)
    #: Set when the app runs under a systemd scope, so teardown can use
    #: `systemctl stop` rather than signals alone.
    systemd_scope: str | None = Field(default=None)


class SystemSetting(SQLModel, table=True):
    """A single instance-wide key/value setting.

    Keys beginning with ``cf_`` hold Cloudflare credentials and are encrypted
    at rest. Keys beginning ``totp_pending_`` hold an in-progress TOTP
    enrolment and are deleted once confirmed.
    """

    key: str = Field(primary_key=True)
    value: str = Field(default="")


class CfRoute(SQLModel, table=True):
    """A public hostname published for one of an app's local ports."""

    id: int | None = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    #: Which of the app's ports this hostname maps to.
    local_port: int | None = Field(default=None)
    hostname: str = Field(default="")
    #: Non-null while the route is live in the tunnel's ingress config.
    cf_route_id: str | None = Field(default=None)


def require_id(value: int | None) -> int:
    """Narrow a model's primary key from ``Optional[int]`` to ``int``.

    SQLModel types every primary key as optional because it is unset until the
    row is first committed, but a row that was *read* from the database always
    has one. This states that invariant in a way the type checker accepts and
    that — unlike ``assert`` — survives ``python -O``.

    :raises ValueError: if the row was never persisted.
    """
    if value is None:
        raise ValueError("This record has no primary key — it was never committed.")
    return value
