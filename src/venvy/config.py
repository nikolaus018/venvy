"""Process-wide configuration, resolved from the environment.

Everything Venvy needs to know about *where* it stores things and *how* it is
exposed lives here, so that no other module reads ``os.environ`` directly.

All settings are read once, when :data:`settings` is first constructed. Tests
that need a different data directory should construct their own
:class:`Settings` and install it with :func:`configure`, rather than mutating
the environment after import.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings", "configure", "get_settings", "settings"]


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    Accepts ``1/true/yes/on`` (case-insensitive) as true and ``0/false/no/off``
    as false. Anything else falls back to *default*.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_csv(name: str) -> list[str]:
    """Read a comma-separated environment variable into a list of clean strings."""
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _default_data_dir() -> Path:
    """Root for mutable state.

    Defaults to the working directory so that `cd /srv/venvy && venvy` behaves
    the way operators expect; ``VENVY_DATA_DIR`` overrides it.
    """
    override = os.getenv("VENVY_DATA_DIR")
    return Path(override) if override else Path.cwd()


def _default_assets_dir() -> Path:
    """Locate the bundled frontend.

    Under PyInstaller the assets are unpacked into ``sys._MEIPASS``; otherwise
    they ship inside the installed package as ``venvy/web``.
    """
    if getattr(sys, "frozen", False):
        # Set by PyInstaller in a frozen build; absent otherwise.
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).parent / "web"


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of Venvy's runtime configuration."""

    # ── Filesystem layout ────────────────────────────────────────────────────
    #: Root for all mutable state: the database, per-app directories, backups
    #: and the local encryption key.
    data_dir: Path = field(default_factory=_default_data_dir)
    #: Directory holding the static frontend (index.html and its assets).
    assets_dir: Path = field(default_factory=_default_assets_dir)

    # ── HTTP server ──────────────────────────────────────────────────────────
    host: str = field(
        default_factory=lambda: os.getenv("VENVY_HOST", os.getenv("HOST", "127.0.0.1"))
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("VENVY_PORT", os.getenv("PORT", "54783")))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv("VENVY_LOG_LEVEL", os.getenv("LOG_LEVEL", "info"))
    )

    # ── Sessions & cookies ───────────────────────────────────────────────────
    #: 30 days — long enough that the installed PWA does not sign users out.
    session_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("VENVY_SESSION_TTL", str(60 * 60 * 24 * 30)))
    )
    #: Default off so plain-HTTP LAN deployments work out of the box. Turn on
    #: whenever Venvy is reachable over HTTPS.
    session_cookie_secure: bool = field(
        default_factory=lambda: _env_flag("SESSION_COOKIE_SECURE", False)
    )

    # ── Access control ───────────────────────────────────────────────────────
    #: Optional IP allowlist. Empty means "no allowlist" (allow everything).
    ip_allowlist: frozenset[str] = field(
        default_factory=lambda: frozenset(_env_csv("IP_ALLOWLIST"))
    )
    #: CORS origins. Empty means same-origin only, which is the safe default.
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(_env_csv("ALLOWED_ORIGINS"))
    )

    # ── Rate limiting ────────────────────────────────────────────────────────
    login_max_attempts: int = field(
        default_factory=lambda: int(os.getenv("VENVY_LOGIN_MAX_ATTEMPTS", "5"))
    )
    login_lockout_seconds: int = field(
        default_factory=lambda: int(os.getenv("VENVY_LOGIN_LOCKOUT", "300"))
    )
    mutation_max_per_window: int = field(
        default_factory=lambda: int(os.getenv("VENVY_MUTATION_LIMIT", "60"))
    )
    mutation_window_seconds: int = field(
        default_factory=lambda: int(os.getenv("VENVY_MUTATION_WINDOW", "60"))
    )

    # ── Limits ───────────────────────────────────────────────────────────────
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.getenv("VENVY_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
    )
    #: An app's log file is rotated once it grows past this size.
    log_rotate_bytes: int = field(
        default_factory=lambda: int(os.getenv("VENVY_LOG_ROTATE_BYTES", str(5 * 1024 * 1024)))
    )
    #: How many rotated log files to keep per app.
    log_rotate_keep: int = field(
        default_factory=lambda: int(os.getenv("VENVY_LOG_ROTATE_KEEP", "5"))
    )

    # ── Host integration ─────────────────────────────────────────────────────
    #: When true, Venvy may run `apt-get install` at startup to provision
    #: python3-venv / cargo. Off by default: a server process should not
    #: silently mutate host packages. When off, a missing toolchain is reported
    #: as a clear error with the command to run instead.
    auto_install_system_deps: bool = field(
        default_factory=lambda: _env_flag("VENVY_AUTO_INSTALL_SYSTEM_DEPS", False)
    )
    #: When true (the default), apps launch under a transient systemd scope so
    #: their CPU and memory limits are actually enforced.
    use_systemd_scopes: bool = field(default_factory=lambda: _env_flag("VENVY_USE_SYSTEMD", True))

    # ── Derived paths ────────────────────────────────────────────────────────
    @property
    def apps_dir(self) -> Path:
        """Root directory holding one subtree per managed app."""
        return self.data_dir / "apps"

    @property
    def backups_dir(self) -> Path:
        """Destination for `POST /api/system/backup` snapshots."""
        return self.data_dir / "backups"

    @property
    def db_file(self) -> Path:
        """Path to the SQLite database file."""
        return self.data_dir / "data.db"

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for :attr:`db_file`."""
        return f"sqlite:///{self.db_file}"

    @property
    def key_file(self) -> Path:
        """Local symmetric key protecting secrets at rest.

        Must be backed up alongside the database; without it the encrypted
        columns are unrecoverable.
        """
        return self.data_dir / ".venvy_key"

    @property
    def index_html(self) -> Path:
        """The SPA entry document."""
        return self.assets_dir / "index.html"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the active settings, constructing them on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def configure(new_settings: Settings) -> Settings:
    """Replace the active settings.

    Intended for tests and for the CLI, which overrides host/port from argv.
    Call before the application is created; modules that cache derived state
    (the database engine, the encryption key) read settings at first use.
    """
    global _settings
    _settings = new_settings
    return _settings


class _SettingsProxy:
    """Attribute proxy so ``settings.x`` always reflects the active settings.

    Modules import ``settings`` once at import time; without this indirection a
    later :func:`configure` call would not be visible to them.
    """

    def __getattr__(self, name: str) -> object:
        return getattr(get_settings(), name)

    def __repr__(self) -> str:
        return repr(get_settings())


#: The process-wide configuration. Import this, not :class:`Settings`.
settings: Settings = _SettingsProxy()  # type: ignore[assignment]
