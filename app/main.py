import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _check_dependencies():
    missing = []
    deps = [
        "fastapi",
        "uvicorn",
        "sqlmodel",
        "pydantic",
        "aiofiles",
        "aiohttp",
        "psutil",
        "multipart",
        "passlib",
        "pyotp",
        "qrcode",
        "websockets",
        "bcrypt",
    ]
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)

    if missing:
        print(
            "\n" + "=" * 60 + "\n"
            "Venvy backend — dependency check.\n"
            "If a required package is missing, we print a clear error instead of\n"
            "silently installing at runtime (which can corrupt a running venv and\n"
            "blocks the event loop on startup).\n\n"
            f"Missing required packages: {', '.join(missing)}\n"
            "Please install them (e.g. pip install -r requirements.txt)\n"
            + "=" * 60
            + "\n",
            file=sys.stderr,
        )
        sys.exit(1)


_check_dependencies()

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.database import create_db_and_tables
from app.routers.apps import router as apps_router
from app.routers.auth import router as auth_router
from app.routers.static import router as static_router
from app.routers.users import router as users_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()

    # Start the background supervisor task (crash detection, restart policies,
    # port detection, session cleanup).
    from app.utils.process import supervisor_task

    sup_task = asyncio.create_task(supervisor_task())
    try:
        yield
    finally:
        sup_task.cancel()
        try:
            await sup_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Venvy", lifespan=lifespan)

# ---------------------------------------------------------------------------
# CORS — restrict to the same origin (or LAN IP configured via env var).
# Allow all localhost variants so plain-HTTP LAN deployments work.
# TODO(security): In a multi-tenant deployment, restrict ALLOWED_ORIGINS to
# the exact production domain only.
# ---------------------------------------------------------------------------
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "")
if _allowed_origins_env.strip():
    _cors_origins = [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
else:
    # Default: allow any localhost / 127.0.0.1 origin for local-LAN deployments.
    # For LAN access the browser origin is the server's own IP:port, which we
    # cannot know at startup, so we use allow_origin_regex.
    _cors_origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Allow any http(s)://127.0.0.1:*, http(s)://localhost:*, or LAN IPs
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:\d+)?",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "x-csrf-token"],
    expose_headers=[],
)


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------
@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    # Prevent clickjacking
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Prevent MIME-type sniffing
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # Restrict referrer to same origin
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # Disable unnecessary browser features
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    )
    # Prevent caching of API responses (static assets use their own Cache-Control)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.mount("/static", StaticFiles(directory="static"), name="static")


app.include_router(auth_router, prefix="/api/auth")
app.include_router(users_router, prefix="/api")
app.include_router(apps_router, prefix="/api")
app.include_router(static_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=True)
