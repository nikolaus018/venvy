"""Venvy — a self-hosted application manager for Python and Rust services.

Venvy runs on a single Linux host and gives you a web UI to upload an app,
build it an isolated virtualenv, start and supervise it, tail its logs, edit
its files, manage its dependencies, open a jailed shell into it, and publish
it to the internet through a Cloudflare Tunnel.

The public entry points are:

    from venvy import create_app     # ASGI application factory
    from venvy.config import settings  # process-wide configuration
"""

from __future__ import annotations

__all__ = ["__version__", "create_app"]

__version__ = "4.0.0"


def __getattr__(name: str) -> object:
    # Imported lazily so that `import venvy` stays cheap and side-effect free —
    # pulling in the app factory drags in FastAPI, SQLModel and the whole
    # service layer, which the CLI and tests do not always need.
    if name == "create_app":
        from venvy.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
