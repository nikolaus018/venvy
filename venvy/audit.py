"""Audit trail: who did what, kept in the database (and the log).

Entries older than AUDIT_RETENTION_DAYS are dropped by the supervisor."""
import contextvars
import datetime
import logging
import os

from sqlmodel import Session, delete

AUDIT_RETENTION_DAYS = int(os.getenv("VENVY_AUDIT_RETENTION_DAYS", "180"))

# Set per request by the ASGI middleware in app.py, so an entry says where
# the action came from without every caller having to pass the address along.
_client_ip: contextvars.ContextVar = contextvars.ContextVar("venvy_client_ip", default="")


def set_client_ip(ip: str):
    return _client_ip.set(ip)


def audit_log(username: str, action: str, detail: str = "") -> None:
    """Record an action. Never raises: the audit trail failing must not undo
    what it describes."""
    suffix = f" — {detail}" if detail else ""
    logging.info(f"AUDIT: user={username!r} action={action}{suffix}")
    try:
        from venvy.db import engine
        from venvy.models import AuditEntry
        with Session(engine) as s:
            s.add(AuditEntry(username=username[:128], action=action[:64], detail=detail[:2000], ip=_client_ip.get()[:64]))
            s.commit()
    except Exception as exc:
        logging.warning(f"Could not write the audit entry: {exc}")


def purge_audit() -> int:
    """Drop entries past the retention period; returns how many."""
    from venvy.config import utc_now
    from venvy.db import engine
    from venvy.models import AuditEntry
    cutoff = utc_now() - datetime.timedelta(days=AUDIT_RETENTION_DAYS)
    with Session(engine) as s:
        res = s.exec(delete(AuditEntry).where(AuditEntry.ts < cutoff))
        s.commit()
        return res.rowcount or 0
