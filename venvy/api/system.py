"""System routes: host metrics, ports, settings, backups and restore, job status."""
import asyncio
import datetime
import logging
import os
import socket
import time
from pathlib import Path
from typing import Dict, List, Set

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select, Session

from venvy.audit import audit_log
from venvy.auth import (
    check_admin,
    check_ip_allowlist,
    get_current_user,
    get_download_user,
    rate_limit_mutations,
    require_operator,
    visible_app_ids,
)
from venvy.backup import (
    _adopt_uploaded_backup,
    _backup_job,
    _BACKUP_TYPES,
    _backup_upload_part,
    _begin_operation,
    _FULL_BACKUP_FILENAME_RE,
    operation_status,
    _purge_stale_backup_uploads,
    _resolve_backup_path,
    _restore_job,
)
from venvy.config import BACKUP_DIR, DATA_DIR, psutil
from venvy.crypto import _decrypt_secret
from venvy.db import get_session
from venvy.jobs import _jobs, _start_job
from venvy.models import App, SystemSetting, User
from venvy.process import (
    get_server_ipv4,
    host_cpu_percent,
    _psutil_net_connections,
    restore_in_progress,
    set_restore_in_progress,
)
from venvy.safefs import is_safe_path
from venvy.settings import _is_secret_setting, _reject_reserved_setting, _setting_for_storage
from venvy.uploads import _append_upload_chunk

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist)]
)


class SystemSettingReq(BaseModel):
    value: str


@router.get("/system/metrics")
def get_system_metrics(user: User = Depends(check_admin)):
    """Return host-level resource statistics."""
    if psutil is None:
        return {"error": "psutil not installed"}
    
    cpu_percent = host_cpu_percent()
    mem = psutil.virtual_memory()
    # The filesystem Venvy's data is on — apps, uploads and backups fill
    # that one, which need not be the root filesystem.
    disk = psutil.disk_usage(str(DATA_DIR))
    load = [0.0, 0.0, 0.0]
    try:
        load = os.getloadavg()
    except Exception:
        pass
    
    return {
        "cpu": {
            "percent": cpu_percent, 
            "load": load, 
            "cores": psutil.cpu_count(logical=True)
        },
        "memory": {"total": mem.total, "available": mem.available, "used": mem.used, "percent": mem.percent},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": disk.percent},
        "local_ip": get_server_ipv4(),
        "uptime": int(time.time() - psutil.boot_time()) if hasattr(psutil, "boot_time") else 0
    }


@router.post("/system/backup")
async def create_backup(
    admin: User = Depends(check_admin),
    _rl: None = Depends(rate_limit_mutations),
):
    """Create a full disaster-recovery backup — see _create_full_backup_archive().
    Answers {"job_id"}; tarring every app can take well over a minute. 409 if
    a backup or restore is already running. Progress: GET /system/backup/status."""
    _begin_operation("backup", admin.username)
    return _start_job(admin, "backup", lambda: _backup_job(admin))


@router.get("/system/backup/status")
def backup_status(admin: User = Depends(check_admin)):
    """What the running backup or restore is doing right now — or how the
    last one ended. Not tied to whoever started it, so it still answers after
    that browser is gone."""
    return {"operation": operation_status()}


@router.post("/system/backups/upload/chunk")
async def upload_backup_chunk(
    upload_id: str = Form(...),
    filename: str = Form(""),
    offset: int = Form(...),
    total: int = Form(...),
    chunk: UploadFile = File(...),
    admin: User = Depends(check_admin),
    # Not rate_limit_mutations, for the same reason as file upload chunks.
):
    """Append one chunk of a backup being uploaded (same protocol as
    /apps/{id}/files/upload/chunk). The last chunk checks that the file is a
    Venvy backup and adds it to the backup list; nothing is restored yet."""
    if restore_in_progress():
        raise HTTPException(409, "A restore is in progress.")
    if total <= 0 or offset < 0 or offset > total:
        raise HTTPException(400, "Invalid offset/total.")
    part = _backup_upload_part(admin.id, upload_id)

    def _append() -> int:
        BACKUP_DIR.mkdir(mode=0o700, exist_ok=True)
        if offset == 0:
            _purge_stale_backup_uploads()
        return _append_upload_chunk(part, chunk, offset, total)

    received = await asyncio.to_thread(_append)
    if received < total:
        return {"ok": True, "received": received, "done": False}
    dest = await asyncio.to_thread(_adopt_uploaded_backup, part, filename)
    audit_log(admin.username, "system_backup_upload", f"path={dest}")
    return {"ok": True, "received": received, "done": True,
            "filename": dest.name, "size_bytes": dest.stat().st_size}


@router.delete("/system/backups/upload/{upload_id}")
def cancel_backup_upload(upload_id: str, admin: User = Depends(check_admin)):
    """Drop the partial file of a backup upload that was cancelled."""
    _backup_upload_part(admin.id, upload_id).unlink(missing_ok=True)
    return {"ok": True}


@router.get("/system/backups")
def list_backups(admin: User = Depends(check_admin)):
    """List existing backups of both kinds — see _BACKUP_TYPES."""
    if not BACKUP_DIR.exists():
        return []
    entries = [
        {
            "filename": f.name,
            "size_bytes": f.stat().st_size,
            "created_at": datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc).isoformat(),
            "type": kind,
        }
        for pattern, kind in _BACKUP_TYPES
        for f in BACKUP_DIR.glob(pattern)
    ]
    return sorted(entries, key=lambda b: b["created_at"], reverse=True)


@router.get("/system/backups/{filename}/download")
def download_backup(filename: str, admin: User = Depends(get_download_user)):
    """Download a previously created backup."""
    if admin.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    path = _resolve_backup_path(filename)
    audit_log(admin.username, "system_backup_download", f"path={path}")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma":        "no-cache",
        },
    )


@router.delete("/system/backups/{filename}")
def delete_backup(
    filename: str,
    admin: User = Depends(check_admin),
    _rl: None = Depends(rate_limit_mutations),
):
    """Delete a backup."""
    if restore_in_progress():
        raise HTTPException(409, "A restore is in progress — backups can't be deleted until it has finished.")
    path = _resolve_backup_path(filename)
    path.unlink()
    audit_log(admin.username, "system_backup_delete", f"path={path}")
    return {"ok": True}


class RestoreBackupReq(BaseModel):
    confirm: str


@router.post("/system/backup/restore/{filename}")
async def restore_backup(
    filename: str,
    req: RestoreBackupReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
    _rl: None = Depends(rate_limit_mutations),
):
    """Roll this live server back to a full backup: stops every running app,
    replaces data.db / apps/ / the encryption key with the backup's
    contents, then restarts the Venvy process so it comes back up clean.

    Destructive and only partly undoable — a fresh full backup of the
    CURRENT state is taken automatically before anything else is touched,
    so the restore itself can be reversed, but any apps running at the time
    are stopped and stay stopped. Requires confirm == "RESTORE" or this is
    a no-op 400; only full backups (not legacy DB-only ones) are restorable.
    """
    if req.confirm != "RESTORE":
        raise HTTPException(400, 'Confirmation phrase did not match — send {"confirm": "RESTORE"}.')
    if restore_in_progress():
        raise HTTPException(409, "A restore is already in progress.")
    if not _FULL_BACKUP_FILENAME_RE.match(filename):
        raise HTTPException(400, "Only full backups can be restored — legacy database-only backups are download-only.")
    archive_path = BACKUP_DIR / filename
    if not is_safe_path(BACKUP_DIR, archive_path) or not archive_path.is_file():
        raise HTTPException(404, "Backup not found.")

    _begin_operation("restore", admin.username, filename)
    audit_log(admin.username, "system_restore_started", f"path={archive_path}")
    session.close()
    set_restore_in_progress(True)
    return _start_job(admin, "restore", lambda: _restore_job(admin, archive_path))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: User = Depends(get_current_user)):
    """Status of a background job: {status: running|done|error, result, error}."""
    job = _jobs.get(job_id)
    if not job or (job["user_id"] != user.id and user.role != "admin"):
        raise HTTPException(404, "Job not found (finished jobs are kept for an hour).")
    return {k: job[k] for k in ("id", "kind", "status", "result", "error", "code")}


@router.get("/system/info")
async def system_info(_: User = Depends(get_current_user)):
    """Return the server's LAN IPv4 address."""
    ipv4 = await asyncio.to_thread(get_server_ipv4)
    return {"ipv4": ipv4}


@router.get("/system/ports")
async def system_ports(
    user: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """Return all currently listening TCP ports plus recommended free ports."""
    def _collect_listening() -> List[Dict]:
        listening: List[Dict] = []
        try:
            if psutil:
                for conn in _psutil_net_connections(kind="tcp"):
                    if conn.status == "LISTEN" and conn.laddr:
                        listening.append({"port": conn.laddr.port, "pid": conn.pid})
            else:
                for path in ("/proc/net/tcp", "/proc/net/tcp6"):
                    try:
                        lines = Path(path).read_text().splitlines()[1:]
                        for line in lines:
                            parts = line.split()
                            if len(parts) < 4 or parts[3] != "0A":
                                continue
                            port_hex = parts[1].split(":")[1]
                            listening.append({"port": int(port_hex, 16), "pid": None})
                    except Exception:
                        pass
        except Exception as exc:
            logging.warning(f"system_ports collect error: {exc}")
        return listening

    def _find_free_ports(used: Set[int], count: int = 12) -> List[int]:
        CANDIDATE_RANGES = [
            (3000, 3010), (4000, 4010), (5000, 5010),
            (8000, 8020), (8080, 8090), (8888, 8898),
            (9000, 9010), (9090, 9100),
        ]
        free: List[int] = []
        for start, end in CANDIDATE_RANGES:
            for port in range(start, end):
                if port in used or len(free) >= count:
                    continue
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        s.settimeout(0)
                        s.bind(("", port))
                        free.append(port)
                except OSError:
                    pass
        return free

    listening_raw, app_rows = await asyncio.gather(
        asyncio.to_thread(_collect_listening),
        asyncio.to_thread(lambda: session.exec(select(App)).all()),
    )

    seen: Set[int] = set()
    result = []
    for entry in sorted(listening_raw, key=lambda x: x["port"]):
        if entry["port"] not in seen:
            seen.add(entry["port"])
            result.append(entry)

    allowed = visible_app_ids(session, user)
    app_port_map: Dict[int, str] = {a.port: a.name for a in app_rows
                                    if a.port and (allowed is None or a.id in allowed)}
    for entry in result:
        entry["app_name"] = app_port_map.get(entry["port"])

    free_ports = await asyncio.to_thread(_find_free_ports, seen)

    return {"listening": result, "free": free_ports}


@router.get("/system/settings/{key}")
def get_system_setting(
    key: str,
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    setting = session.get(SystemSetting, key)
    if _is_secret_setting(key):
        # Write-only: a credential has no business travelling back to a
        # browser. The server uses it; the UI only needs to know it exists.
        return {"key": key, "value": "", "is_set": bool(setting and setting.value)}
    if not setting:
        return {"key": key, "value": ""}
    value = setting.value
    if key.startswith("cf_"):
        value = _decrypt_secret(value)
    return {"key": key, "value": value}


@router.put("/system/settings/{key}")
def set_system_setting(
    key: str,
    req: SystemSettingReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    _reject_reserved_setting(key)
    value, stored_value = _setting_for_storage(key, req.value)
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = stored_value
    else:
        setting = SystemSetting(key=key, value=stored_value)
    session.add(setting)
    session.commit()
    audit_log(admin.username, "system_setting_change", f"key={key}")
    return {"ok": True, "stored_length": len(value)}


class BatchSettingItem(BaseModel):
    key: str
    value: str


@router.post("/system/settings/batch")
def set_system_settings_batch(
    items: List[BatchSettingItem],
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Save multiple settings in a single DB transaction."""
    for item in items:
        _reject_reserved_setting(item.key)
    for item in items:
        _, stored_value = _setting_for_storage(item.key, item.value)
        setting = session.get(SystemSetting, item.key)
        if setting:
            setting.value = stored_value
        else:
            setting = SystemSetting(key=item.key, value=stored_value)
        session.add(setting)
    session.commit()
    audit_log(admin.username, "system_setting_change", f"batch keys={[i.key for i in items]}")
    return {"ok": True, "saved": len(items)}
