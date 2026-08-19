"""Engine lifecycle and session helpers.

The engine is created lazily on first use rather than at import time. That
keeps ``import venvy`` free of side effects (no database file appears just
because something imported a model) and lets tests point Venvy at a temporary
directory via :func:`venvy.config.configure` before anything connects.
"""

from __future__ import annotations

import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from venvy.config import settings

__all__ = ["dispose_engine", "get_engine", "get_session", "session_scope"]

logger = logging.getLogger(__name__)

_engine: Engine | None = None

#: SQLite tuning applied to every connection.
#:   journal_mode=WAL  — readers never block the writer, which matters because
#:                       the supervisor polls while requests are being served.
#:   synchronous=NORMAL — durable enough for this workload, far fewer fsyncs.
#:   foreign_keys=ON   — SQLite disables them per-connection by default.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-10000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=20000",
)


def _apply_pragmas(dbapi_connection, _connection_record) -> None:
    """Configure each new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            settings.database_url,
            # Sessions are handed between threads by the thread-pool executor
            # that runs sync route handlers, so SQLite's own thread check has
            # to be turned off; concurrency is instead handled by WAL plus the
            # busy timeout above.
            connect_args={"check_same_thread": False, "timeout": 20},
            echo=False,
        )
        event.listen(_engine, "connect", _apply_pragmas)
        logger.debug("Database engine created for %s", settings.db_file)
    return _engine


def dispose_engine() -> None:
    """Close all pooled connections and drop the cached engine.

    Used at shutdown and by tests that swap data directories mid-process.
    """
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    with Session(get_engine()) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for a session outside the request cycle.

    Used by background tasks — the supervisor, process reapers — which have no
    dependency-injection container to borrow a session from.
    """
    with Session(get_engine()) as session:
        yield session
