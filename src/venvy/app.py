"""The ASGI application factory.

Exposes :func:`create_app` plus a module-level ``app`` for ``uvicorn
venvy.app:app``. Building the application in a function — rather than at import
time — lets tests construct an isolated instance per test with its own data
directory.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response

from venvy import __version__
from venvy.api import api_router, auth_router, health, terminal
from venvy.config import settings
from venvy.lifespan import lifespan
from venvy.security.middleware import CSRFMiddleware, SecurityHeadersMiddleware

__all__ = ["app", "create_app"]

logger = logging.getLogger(__name__)

_DESCRIPTION = """
Venvy is a self-hosted application manager. It builds an isolated environment
for each app you upload, supervises the process, streams its logs, and can
publish it to the internet through a Cloudflare Tunnel.
"""


def create_app() -> FastAPI:
    """Build and configure the ASGI application."""
    application = FastAPI(
        title="Venvy",
        version=__version__,
        description=_DESCRIPTION.strip(),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    _configure_middleware(application)

    application.include_router(health.router)
    application.include_router(auth_router)
    application.include_router(api_router)
    application.include_router(terminal.router)
    _register_frontend(application)

    return application


def _configure_middleware(application: FastAPI) -> None:
    """Install the middleware stack.

    Order matters — Starlette runs middleware in reverse registration order, so
    the CSRF check added last is the outermost and rejects a forged request
    before anything else does work for it.
    """
    origins = list(settings.allowed_origins)
    if not origins:
        logger.info(
            "ALLOWED_ORIGINS is not set — only same-origin requests are accepted. "
            "Set it (e.g. ALLOWED_ORIGINS='https://venvy.example.com') if the UI is "
            "served from a different origin than the API."
        )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(SecurityHeadersMiddleware)
    application.add_middleware(CSRFMiddleware)


def _register_frontend(application: FastAPI) -> None:
    """Serve the single-page frontend and its static assets.

    Assets are mounted only if the directory exists, so an API-only deployment
    (or a test that never ships the frontend) starts cleanly instead of raising
    at import time.
    """
    assets_dir = settings.assets_dir

    for subdirectory in ("js", "styles"):
        path = assets_dir / subdirectory
        if path.is_dir():
            from fastapi.staticfiles import StaticFiles

            application.mount(
                f"/{subdirectory}", StaticFiles(directory=str(path)), name=subdirectory
            )
        else:
            logger.warning("Frontend directory %s is missing — the UI may not load.", path)

    @application.get("/", include_in_schema=False)
    def serve_spa() -> Response:
        """Serve the SPA entry document."""
        index = settings.index_html
        if index.exists():
            return FileResponse(
                index,
                # The HTML shell references versioned module paths; caching it
                # would pin users to a stale asset graph after an upgrade.
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )
        logger.error("index.html was not found at %s", index)
        return PlainTextResponse(
            "Venvy frontend not found. Reinstall the package or check VENVY_ASSETS_DIR.",
            status_code=404,
        )


#: Module-level instance for `uvicorn venvy.app:app`.
app = create_app()
