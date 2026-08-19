"""Schema creation and forward-only migrations.

Venvy targets a single-file SQLite database on one host, so it does not carry
Alembic. Instead every schema change is expressed as an idempotent ``ALTER
TABLE`` that is attempted on each startup: on an up-to-date database it fails
with "duplicate column" and is ignored, and on an older one it applies. Any
*other* failure is surfaced as a warning, because that is a real problem an
operator needs to see.

Adding a migration: append to :data:`_COLUMN_MIGRATIONS`. Never edit or reorder
an existing entry — a deployed database may already have applied it.
"""

from __future__ import annotations

import logging

from sqlalchemy import Connection
from sqlmodel import SQLModel

from venvy.config import settings
from venvy.db.engine import get_engine

__all__ = ["run_migrations"]

logger = logging.getLogger(__name__)

#: Idempotent additive column migrations, in application order.
_COLUMN_MIGRATIONS = (
    "ALTER TABLE app ADD COLUMN runtime VARCHAR DEFAULT 'python'",
    "ALTER TABLE app ADD COLUMN url_source_pref VARCHAR DEFAULT 'local'",
    "ALTER TABLE cfroute ADD COLUMN local_port INTEGER",
    "ALTER TABLE app ADD COLUMN manual_ports VARCHAR DEFAULT '[]'",
    "ALTER TABLE app ADD COLUMN memory_limit_mb INTEGER DEFAULT 512",
    "ALTER TABLE app ADD COLUMN cpu_limit_percent INTEGER DEFAULT 50",
    "ALTER TABLE app ADD COLUMN systemd_scope VARCHAR",
)

#: Substrings identifying "this migration already ran", which is not an error.
_ALREADY_APPLIED = ("duplicate column", "already exists")


def _is_already_applied(exc: Exception) -> bool:
    """Return True if *exc* just means the migration had already been applied."""
    message = str(exc).lower()
    return any(marker in message for marker in _ALREADY_APPLIED)


def _try_migration(conn: Connection, sql: str) -> bool:
    """Run one migration. Returns True if it applied, False if already present."""
    try:
        conn.exec_driver_sql(sql)
        return True
    except Exception as exc:
        if not _is_already_applied(exc):
            logger.warning("Migration failed: %s -> %s", sql, exc)
        return False


def _backfill_roles(conn: Connection) -> None:
    """Populate ``user.role`` from the legacy ``is_admin`` boolean.

    Only reachable on a database created before roles existed: on a fresh
    database ``create_all`` has already made the column, so the ALTER that
    guards this reports "duplicate column" and we never get here.
    """
    try:
        columns = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()]
        if "is_admin" not in columns:
            return
        conn.exec_driver_sql(
            "UPDATE user SET role = CASE WHEN is_admin = 1 THEN 'admin' ELSE 'operator' END"
        )
        logger.info("Migrated existing users from is_admin to the role-based access model.")
    except Exception as exc:
        logger.warning(
            "Role backfill from legacy is_admin failed — existing users default to "
            "'operator' and can be corrected by an admin: %s",
            exc,
        )


def run_migrations() -> None:
    """Create any missing tables, then apply pending column migrations."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)

    with engine.begin() as conn:
        for sql in _COLUMN_MIGRATIONS:
            _try_migration(conn, sql)

        # Handled apart from the loop because whether it *applied* decides
        # whether the one-time backfill below should run.
        role_column_is_new = _try_migration(
            conn, "ALTER TABLE user ADD COLUMN role VARCHAR DEFAULT 'operator'"
        )
        if role_column_is_new:
            _backfill_roles(conn)

    settings.apps_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Schema is up to date.")
