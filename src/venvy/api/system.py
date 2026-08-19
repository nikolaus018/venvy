"""Host information, metrics, settings and database backups."""

from __future__ import annotations

import asyncio
import datetime
import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from venvy.config import settings
from venvy.core.audit import audit_log
from venvy.core.crypto import decrypt_secret, encrypt_secret
from venvy.core.net import get_server_ipv4, list_listening_sockets, suggest_free_ports
from venvy.core.timeutils import utc_now
from venvy.db.engine import get_session
from venvy.db.models import App, SystemSetting, User
from venvy.schemas import BatchSettingItem, SystemSettingRequest
from venvy.security.dependencies import check_admin, get_current_user, require_operator
from venvy.security.rate_limit import rate_limit_mutations
from venvy.services import cloudflare as cf
from venvy.services import metrics

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])

#: Settings whose values are encrypted at rest.
_ENCRYPTED_KEY_PREFIX = "cf_"
#: Settings that are internal state and must never be read or written via the
#: settings API — a pending TOTP secret is mid-enrolment credential material.
_PRIVATE_KEY_PREFIXES = ("totp_pending_",)


def _reject_private_key(key: str) -> None:
    """Refuse access to internal settings keys."""
    if key.startswith(_PRIVATE_KEY_PREFIXES):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That setting is not accessible.")


def _store_value(key: str, value: str) -> str:
    """Prepare *value* for storage, encrypting and sanitising where required."""
    if key.startswith(_ENCRYPTED_KEY_PREFIX):
        return encrypt_secret(cf.sanitize_credential(value))
    return value


@router.get("/info")
async def system_info(_user: User = Depends(get_current_user)):
    """Return the host's LAN IPv4 address."""
    return {"ipv4": await asyncio.to_thread(get_server_ipv4)}


@router.get("/metrics")
def get_system_metrics(_admin: User = Depends(check_admin)):
    """Return host CPU, memory, disk and uptime."""
    snapshot = metrics.host_snapshot()
    if snapshot is None:
        return {"error": "psutil is not installed — host metrics are unavailable."}

    from venvy.core.process_compat import psutil

    snapshot["local_ip"] = get_server_ipv4()
    snapshot["uptime"] = (
        int(datetime.datetime.now().timestamp() - psutil.boot_time())
        if hasattr(psutil, "boot_time")
        else 0
    )
    return snapshot


@router.get("/ports")
async def system_ports(
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Return every listening TCP port, annotated with the app that owns it,
    plus a handful of free ports to suggest for a new app."""
    listening, apps = await asyncio.gather(
        asyncio.to_thread(list_listening_sockets),
        asyncio.to_thread(lambda: list(session.exec(select(App)).all())),
    )

    # A port can be reported once per socket; the UI wants one row per port.
    seen: set[int] = set()
    unique = []
    for entry in sorted(listening, key=lambda item: item["port"]):
        if entry["port"] not in seen:
            seen.add(entry["port"])
            unique.append(entry)

    owners = {app.port: app.name for app in apps if app.port}
    for entry in unique:
        entry["app_name"] = owners.get(entry["port"])

    free = await asyncio.to_thread(suggest_free_ports, seen)
    return {"listening": unique, "free": free}


# ── Settings ─────────────────────────────────────────────────────────────────


@router.get("/settings/{key}")
def get_system_setting(
    key: str,
    session: Session = Depends(get_session),
    _admin: User = Depends(check_admin),
):
    """Read one setting, decrypting it when it is stored encrypted."""
    _reject_private_key(key)
    setting = session.get(SystemSetting, key)
    if not setting:
        return {"key": key, "value": ""}

    value = setting.value
    if key.startswith(_ENCRYPTED_KEY_PREFIX):
        value = decrypt_secret(value)
    return {"key": key, "value": value}


@router.put("/settings/{key}")
def set_system_setting(
    key: str,
    request: SystemSettingRequest,
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Write one setting."""
    _reject_private_key(key)
    stored = _store_value(key, request.value)

    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = stored
    else:
        setting = SystemSetting(key=key, value=stored)
    session.add(setting)
    session.commit()

    audit_log(admin.username, "system_setting_change", f"key={key}")
    return {"ok": True, "stored_length": len(request.value)}


@router.post("/settings/batch")
def set_system_settings_batch(
    items: list[BatchSettingItem],
    session: Session = Depends(get_session),
    admin: User = Depends(check_admin),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Write several settings in one transaction.

    Used by the Cloudflare settings form, where saving the token without the
    account id would leave the integration in a half-configured state.
    """
    for item in items:
        _reject_private_key(item.key)

    for item in items:
        stored = _store_value(item.key, item.value)
        setting = session.get(SystemSetting, item.key)
        if setting:
            setting.value = stored
        else:
            setting = SystemSetting(key=item.key, value=stored)
        session.add(setting)
    session.commit()

    audit_log(admin.username, "system_setting_change", f"batch keys={[item.key for item in items]}")
    return {"ok": True, "saved": len(items)}


# ── Backups ──────────────────────────────────────────────────────────────────


@router.post("/backup")
def create_backup(
    admin: User = Depends(check_admin),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Snapshot the database with ``VACUUM INTO``.

    Safe to call while Venvy is running. The database is in WAL mode, so a raw
    ``cp`` can miss committed data still sitting in the ``-wal`` file;
    ``VACUUM INTO`` reads inside its own transaction and always produces a
    consistent single file.

    Covers the database only. The ``apps/`` tree and ``.venvy_key`` must be
    backed up separately — see docs/operations.md.
    """
    backup_dir = settings.backups_dir
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"data-{utc_now().strftime('%Y%m%d-%H%M%S')}.db"

    if destination.exists():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A backup with this timestamp already exists — try again in a second.",
        )

    try:
        connection = sqlite3.connect(str(settings.db_file))
        try:
            connection.execute("VACUUM INTO ?", (str(destination),))
        finally:
            connection.close()
    except sqlite3.Error as exc:
        logger.error("Database backup failed: %s", exc)
        destination.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"Backup failed: {exc}") from exc

    audit_log(admin.username, "system_backup", f"path={destination}")
    return {"ok": True, "path": str(destination), "size_bytes": destination.stat().st_size}


@router.get("/backups")
def list_backups(_admin: User = Depends(check_admin)):
    """List existing database snapshots, newest first."""
    backup_dir = settings.backups_dir
    if not backup_dir.exists():
        return []

    entries = []
    for path in backup_dir.glob("data-*.db"):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.UTC
                ).isoformat(),
            }
        )
    return sorted(entries, key=lambda entry: entry["created_at"], reverse=True)


# ── Cloudflare credential checks ─────────────────────────────────────────────


@router.post("/cloudflare/test")
async def test_cf_credentials(
    session: Session = Depends(get_session),
    _admin: User = Depends(check_admin),
):
    """Verify the saved Cloudflare credentials and the token's permissions."""
    return await cf.verify_credentials(cf.get_credentials(session))


@router.get("/cloudflare/zones")
async def list_cf_zones(
    session: Session = Depends(get_session),
    _user: User = Depends(require_operator),
):
    """List the account's active zones, for the hostname picker.

    Operator rather than admin: publishing a tunnel is an operator action, and
    the hostname picker needs this list to offer the account's domains.
    """
    credentials = cf.get_credentials(session)
    return {"ok": True, "zones": await cf.list_zones(credentials.api_token)}
