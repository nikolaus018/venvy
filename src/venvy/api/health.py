"""Unauthenticated liveness endpoint for external monitoring."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlmodel import select

from venvy.core.timeutils import utc_now
from venvy.db.engine import session_scope
from venvy.db.models import SystemSetting
from venvy.services import registry

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/api/health")
def health_check() -> JSONResponse:
    """Report whether this instance is serving traffic.

    Deliberately outside the API router, so it is subject to neither the IP
    allowlist nor session auth — an uptime monitor has to reach it without
    being able to sign in.

    Performs a trivial database round-trip rather than returning a static
    ``ok``, so a wedged or corrupted database is reported as degraded (503)
    instead of a false healthy.
    """
    try:
        with session_scope() as session:
            session.exec(select(SystemSetting).limit(1)).first()
        database_ok = True
    except Exception as exc:
        logger.error("Health check database probe failed: %s", exc)
        database_ok = False

    return JSONResponse(
        {
            "ok": database_ok,
            "status": "healthy" if database_ok else "degraded",
            "uptime_seconds": int((utc_now() - registry.BOOT_TIME).total_seconds()),
        },
        status_code=200 if database_ok else 503,
    )
