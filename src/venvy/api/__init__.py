"""HTTP API routers, grouped by resource.

Two top-level routers are assembled here:

* ``auth_router`` — ``/api/auth/*``, reachable before sign-in.
* ``api_router``  — ``/api/*``, everything else.

Both carry the IP allowlist dependency. ``/api/health`` is deliberately
registered outside them, in :mod:`venvy.app`, so an uptime monitor can reach it
without being on the allowlist or holding a session.
"""

from fastapi import APIRouter, Depends

from venvy.api import apps, cloudflare, files, logs, packages, system, users
from venvy.api import auth as auth_api
from venvy.security.dependencies import check_ip_allowlist

__all__ = ["api_router", "auth_router"]

auth_router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[Depends(check_ip_allowlist)],
)
auth_router.include_router(auth_api.router)

api_router = APIRouter(
    prefix="/api",
    dependencies=[Depends(check_ip_allowlist)],
)
api_router.include_router(users.router)
api_router.include_router(apps.router)
api_router.include_router(files.router)
api_router.include_router(packages.router)
api_router.include_router(logs.router)
api_router.include_router(cloudflare.router)
api_router.include_router(system.router)
