"""Logging setup.

Called once from the CLI. Importing Venvy never configures logging on its own,
so embedding the ASGI app in another process leaves that process's logging
configuration alone.
"""

from __future__ import annotations

import logging
import sys

__all__ = ["LOG_FORMAT", "configure_logging"]

LOG_FORMAT = "%(asctime)s │ %(levelname)-8s │ %(name)s │ %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = "info") -> None:
    """Install a single stderr handler at *level* on the root logger.

    Existing handlers are replaced so that repeated calls (the CLI, then a test
    harness) do not duplicate every line.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    if not isinstance(numeric, int):
        numeric = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric)

    # These are informative at DEBUG but drown out everything else; SQLAlchemy
    # in particular logs every statement.
    logging.getLogger("sqlalchemy.engine").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("aiosqlite").setLevel(max(numeric, logging.WARNING))
