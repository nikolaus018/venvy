import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import socket
import sys
import time
import unicodedata
from typing import Dict, List, Optional, Set

import aiofiles
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile, WebSocket)
from fastapi.responses import FileResponse
from pathlib import Path
from pydantic import BaseModel
from sqlmodel import Session, select

try:
    import tomllib as _toml
except ImportError:
    _toml = None

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None


from app.auth import (
    check_admin,
    get_current_user,
    _make_tz_aware,
)
from app.config import (
    APPS_BASE_DIR,
    _app_metrics,
    _host_cpu_percent,
    _running_tasks,
    _active_app_pids,
)
from app.database import (
    App,
    CfRoute,
    SystemSetting,
    User,
    UserSession,
    engine,
    get_session,
    utc_now,
    _psutil_net_connections,
)
from app.schemas import (
    AppUpdateReq,
    FileOpReq,
    PackageReq,
    SystemSettingReq,
    TotpDisableReq,
    TotpSetupReq,
    UserReq,
)
from app.utils.network import (
    get_server_ipv4,
    sanitize_slug,
    is_safe_path,
    _cf_delete_dns_cname,
    _cf_find_zone_id,
    _cf_request,
    _cf_upsert_dns_cname,
    CfPublishReq,
)
from app.utils.process import (
    _cancel_log_task,
    _pid_alive,
    _trigger_supervisor,
    start_app_process_logic,
    stop_app_process_logic,
)
from app.utils.terminal import terminal_manager

router = APIRouter()


def _app_safe(app_row: App) -> dict:
    """Return App as dict — injects local_url + access_url + live metrics."""
    d = app_row.model_dump()
    local_url = f"http://{get_server_ipv4()}:{app_row.port}" if app_row.port else None
    d["local_url"] = local_url
    d["metrics"] = _app_metrics.get(app_row.id, {"cpu": 0.0, "memory": 0})
    pref = app_row.url_source_pref
    if pref == "public" and app_row.external_url:
        d["access_url"] = app_row.external_url
        d["url_source"] = "cloudflare"
    elif local_url:
        d["access_url"] = local_url
        d["url_source"] = "local"
    else:
        d["access_url"] = None
        d["url_source"] = None
    for key in (
        "created_at",
        "updated_at",
        "last_start_time",
        "last_stop_time",
        "last_manually_stopped_at",
    ):
        val = d.get(key)
        if isinstance(val, datetime.datetime):
            d[key] = val.isoformat()
    return d


# ---------------------------------------------------------------------------
# App endpoints
# ---------------------------------------------------------------------------


@router.get("/apps")
def list_apps(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    apps = session.exec(
        select(App).where(App.user_id == user.id).order_by(App.name)
    ).all()
    changed = False
    for app_row in apps:
        if app_row.status in ("starting", "stopping"):
            continue
        if app_row.status == "running" and app_row.pid:
            if not _pid_alive(app_row.pid):
                app_row.status = "stopped"
                app_row.pid = None
                app_row.last_stop_time = utc_now()
                session.add(app_row)
                changed = True
        elif app_row.status == "stopped" and app_row.pid:
            app_row.pid = None
            session.add(app_row)
            changed = True
    if changed:
        session.commit()
    return [_app_safe(a) for a in apps]


@router.get("/system/metrics")
def get_system_metrics(user: User = Depends(check_admin)):
    """Return host-level resource statistics."""
    if psutil is None:
        return {"error": "psutil not installed"}

    cpu_percent = _host_cpu_percent
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load = [0.0, 0.0, 0.0]
    try:
        load = os.getloadavg()
    except Exception:
        pass

    return {
        "cpu": {
            "percent": cpu_percent,
            "load": load,
            "cores": psutil.cpu_count(logical=True),
        },
        "memory": {
            "total": mem.total,
            "available": mem.available,
            "used": mem.used,
            "percent": mem.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
        "local_ip": get_server_ipv4(),
        "uptime": (
            int(time.time() - psutil.boot_time()) if hasattr(psutil, "boot_time") else 0
        ),
    }


@router.post("/apps/upload")
async def upload_app(
    name: str = Form(...),
    runtime: str = Form("python"),
    entrypoint: str = Form(""),
    start_on_boot: bool = Form(False),
    restart_policy: str = Form("never"),
    files: Optional[List[UploadFile]] = File(None),
    requirements: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if runtime not in ("python", "rust"):
        raise HTTPException(400, "runtime must be one of: python, rust")
    if restart_policy not in ("never", "on-failure", "always"):
        raise HTTPException(
            400, "restart_policy must be one of: never, on-failure, always"
        )

    entrypoint_clean = ""
    if entrypoint.strip():
        entrypoint_strip = entrypoint.strip()
        if entrypoint_strip.startswith("/") or ".." in entrypoint_strip.split("/"):
            raise HTTPException(
                400, "Entrypoint must be a relative path without '..'."
            )
        if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", entrypoint_strip):
            raise HTTPException(400, "Entrypoint path contains invalid characters.")
        entrypoint_clean = entrypoint_strip

    slug = sanitize_slug(name)
    user_apps_dir = APPS_BASE_DIR / str(user.id)
    await asyncio.to_thread(user_apps_dir.mkdir, exist_ok=True)

    app_dir = user_apps_dir / slug
    if await asyncio.to_thread(app_dir.exists):
        slug = f"{slug}-{int(time.time())}"
        app_dir = user_apps_dir / slug
    await asyncio.to_thread(app_dir.mkdir, parents=True, mode=0o750)
    await asyncio.to_thread((app_dir / "logs").mkdir, mode=0o750)

    _MAX_UPLOAD_BYTES = 50 * 1024 * 1024
    for file in files or []:
        rel_path = file.filename or "upload"
        if "\0" in rel_path or ".." in rel_path or rel_path.startswith("/"):
            continue

        safe_name = Path(rel_path).name
        if not safe_name or safe_name.startswith(".") or safe_name.startswith(".jail"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.+\-\s()[\]@#!~]+", safe_name):
            continue

        dest = (app_dir / rel_path).resolve()
        if not is_safe_path(app_dir, dest):
            continue

        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)

        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(413, f"File '{safe_name}' exceeds 50 MB limit.")
        async with aiofiles.open(dest, "wb") as f:
            await f.write(data)

    venv_path = app_dir / "venv"
    if runtime == "python":
        proc = await asyncio.create_subprocess_exec(
            "python3",
            "-m",
            "venv",
            str(venv_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, venv_err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(500, "venv creation timed out.")
        if proc.returncode != 0:
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            err_msg = venv_err.decode(errors="replace")
            hint = ""
            if "ensurepip" in err_msg or "venv" in err_msg.lower():
                hint = f" Hint: python3-venv may not be installed — run: sudo apt-get install -y python3.{sys.version_info.minor}-venv"
            raise HTTPException(500, f"venv creation failed: {err_msg}{hint}")

    if requirements and requirements.filename:
        if runtime == "python":
            req_path = app_dir / "requirements.txt"
            async with aiofiles.open(req_path, "wb") as f:
                await f.write(await requirements.read())
            pip_bin = venv_path / "bin" / "pip"
            if not await asyncio.to_thread(pip_bin.exists):
                await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
                raise HTTPException(
                    500, "pip not found in the new virtual environment."
                )
            proc = await asyncio.create_subprocess_exec(
                str(pip_bin),
                "install",
                "--no-input",
                "-r",
                str(req_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, pip_err = await asyncio.wait_for(proc.communicate(), timeout=600)
            except asyncio.TimeoutError:
                proc.kill()
                await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
                raise HTTPException(500, "pip install timed out after 10 minutes.")
            if proc.returncode != 0:
                await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
                raise HTTPException(500, f"pip install failed: {pip_err.decode()}")

    new_app = App(
        user_id=user.id,
        name=name,
        slug=slug,
        runtime=runtime,
        app_dir=str(app_dir),
        venv_path=str(venv_path),
        entrypoint=entrypoint_clean,
        start_on_boot=start_on_boot,
        restart_policy=restart_policy,
        port=None,
    )
    session.add(new_app)
    session.commit()
    session.refresh(new_app)
    return _app_safe(new_app)


@router.post("/apps/{app_id}/control")
async def control_app(
    app_id: int,
    action: str = Query(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "Invalid action. Use: start | stop | restart")

    async def _do_start():
        try:
            with Session(engine) as s:
                await start_app_process_logic(app_id, s)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background start failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a:
                    a.status = "error"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_stop():
        try:
            with Session(engine) as s:
                await stop_app_process_logic(app_id, s, manual=True)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background stop failed for app {app_id}: {exc}")
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_restart():
        try:
            with Session(engine) as s:
                await stop_app_process_logic(app_id, s, manual=False)
            await asyncio.sleep(0.1)
            with Session(engine) as s:
                refreshed = s.get(App, app_id)
                if refreshed and refreshed.pid and _pid_alive(refreshed.pid):
                    refreshed.pid = None
                    refreshed.status = "stopped"
                    s.add(refreshed)
                    s.commit()
            with Session(engine) as s:
                await start_app_process_logic(app_id, s)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background restart failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a:
                    a.status = "error"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    if action == "start":
        if app_row.status != "running":
            app_row.status = "starting"
            session.add(app_row)
            session.commit()
        t = asyncio.create_task(_do_start())
        _running_tasks.add(t)
    elif action == "stop":
        if app_row.status != "stopped":
            app_row.status = "stopping"
            session.add(app_row)
            session.commit()
        t = asyncio.create_task(_do_stop())
        _running_tasks.add(t)
    elif action == "restart":
        app_row.status = "stopping"
        session.add(app_row)
        session.commit()
        t = asyncio.create_task(_do_restart())
        _running_tasks.add(t)

    return {"status": app_row.status, "queued": True}


@router.put("/apps/{app_id}")
async def update_app(
    app_id: int,
    req: AppUpdateReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    if req.name is not None:
        name_clean = req.name.strip()
        if not name_clean:
            raise HTTPException(400, "App name cannot be empty.")
        if len(name_clean) > 50:
            raise HTTPException(400, "App name must be 50 characters or less.")
        app_row.name = name_clean
    if req.start_on_boot is not None:
        app_row.start_on_boot = req.start_on_boot
    if req.restart_policy is not None:
        if req.restart_policy not in ("never", "on-failure", "always"):
            raise HTTPException(
                400, "restart_policy must be one of: never, on-failure, always"
            )
        app_row.restart_policy = req.restart_policy
    if req.port is not None:
        if req.port != 0 and not (1 <= req.port <= 65535):
            raise HTTPException(
                400, "Port must be between 1 and 65535 (or 0 to clear)."
            )
        app_row.port = None if req.port == 0 else req.port
    if req.manual_ports is not None:
        valid_ports = []
        for p in req.manual_ports:
            if not (1 <= p <= 65535):
                raise HTTPException(400, f"Invalid port: {p}")
            valid_ports.append(p)
        app_row.manual_ports = json.dumps(valid_ports)
        if valid_ports:
            app_row.port = valid_ports[0]
        elif req.manual_ports == []:
            app_row.port = None

    manual_list = []
    try:
        manual_list = json.loads(app_row.manual_ports or "[]")
    except Exception:
        pass

    auto_ports = []
    try:
        auto_ports = json.loads(app_row.detected_ports or "[]")
    except Exception:
        pass

    if manual_list:
        combined = manual_list
    else:
        combined = auto_ports

    app_row.detected_ports = json.dumps(combined)
    if not app_row.port and combined:
        app_row.port = combined[0]

    if req.env_vars is not None:
        app_row.env_json = json.dumps(req.env_vars)
    if req.entrypoint is not None:
        ep_strip = req.entrypoint.strip()
        if ep_strip == "":
            app_row.entrypoint = ""
        else:
            if ep_strip.startswith("/") or ".." in ep_strip.split("/"):
                raise HTTPException(
                    400, "Entrypoint must be a relative path without '..'."
                )
            if not re.fullmatch(r"[A-Za-z0-9_.\-/]+", ep_strip):
                raise HTTPException(
                    400, "Entrypoint path contains invalid characters."
                )
            app_row.entrypoint = ep_strip
    if req.url_source_pref is not None:
        if req.url_source_pref not in ("local", "public"):
            raise HTTPException(400, "url_source_pref must be 'local' or 'public'")
        app_row.url_source_pref = req.url_source_pref
    if req.runtime is not None:
        if req.runtime not in ("python", "rust"):
            raise HTTPException(400, "runtime must be one of: python, rust")
        app_row.runtime = req.runtime

    app_row.updated_at = utc_now()
    session.add(app_row)
    session.commit()
    refreshed = session.get(App, app_id)
    return _app_safe(refreshed)


@router.delete("/apps/{app_id}")
async def delete_app(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    _cancel_log_task(app_id)
    await stop_app_process_logic(app_id, session)

    cf_route = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).first()
    if cf_route and cf_route.cf_route_id:
        try:
            creds = _get_cf_creds(session)
            account_id = creds["cf_account_id"]
            tunnel_id = creds["cf_tunnel_id"]
            api_token = creds["cf_api_token"]
            cfg_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                f"/cfd_tunnel/{tunnel_id}/configurations"
            )
            hostname = cf_route.hostname
            current_cfg = await _cf_request("GET", cfg_url, api_token)
            result_obj = current_cfg.get("result") or {}
            ingress = result_obj.get("config", {}).get("ingress") or []
            named = [
                r for r in ingress if r.get("hostname") and r["hostname"] != hostname
            ]
            catch_all = [r for r in ingress if not r.get("hostname")]
            new_ingress = named + (catch_all or [{"service": "http_status:404"}])
            await _cf_request(
                "PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}}
            )
            zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
            if zone_id:
                await _cf_delete_dns_cname(zone_id, hostname, api_token)
            logging.info(f"CF route cleaned up for deleted app {app_id} ({hostname})")
        except Exception as exc:
            logging.warning(f"CF cleanup failed for app {app_id} during delete: {exc}")
    if cf_route:
        session.delete(cf_route)

    app_dir_path = Path(app_row.app_dir)
    if await asyncio.to_thread(app_dir_path.exists):
        await asyncio.to_thread(shutil.rmtree, app_row.app_dir, ignore_errors=True)
    session.delete(app_row)
    session.commit()
    return {"ok": True}


@router.get("/apps/{app_id}/stats")
def app_stats(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    stats: Dict = {"cpu": 0.0, "memory": 0.0, "uptime": 0.0}
    if app_row.pid and psutil:
        try:
            p = psutil.Process(app_row.pid)
            stats["cpu"] = p.cpu_percent(interval=None)
            stats["memory"] = p.memory_info().rss / (1024 * 1024)
            if app_row.last_start_time:
                stats["uptime"] = (
                    utc_now() - _make_tz_aware(app_row.last_start_time)
                ).total_seconds()
        except Exception:
            pass
    return stats


@router.get("/apps/{app_id}/files/tree")
def file_tree(
    app_id: int,
    path: str = "",
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target = (root / path).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(403)
    results = []
    _SKIP_NAMES = {
        "venv",
        "__pycache__",
        "logs",
        "node_modules",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
    }
    if target.is_dir():
        for item in target.iterdir():
            if item.name.startswith(".") or item.name in _SKIP_NAMES:
                continue
            try:
                size = item.stat().st_size
            except OSError:
                size = 0
            results.append(
                {
                    "name": item.name,
                    "path": item.relative_to(root).as_posix(),
                    "is_dir": item.is_dir(),
                    "size": size,
                }
            )
    return sorted(results, key=lambda x: (not x["is_dir"], x["name"].lower()))


@router.post("/apps/{app_id}/files/op")
async def file_operations(
    app_id: int,
    req: FileOpReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target = (root / req.path).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(403)
    if target.name.startswith(".jailrc") or target.name.startswith(".jailsh"):
        raise HTTPException(403, "Operation not permitted on this path.")

    try:
        if req.operation == "create_file":
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(target.touch)

        elif req.operation == "create_folder":
            await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)

        elif req.operation == "delete":
            if await asyncio.to_thread(target.is_dir):
                await asyncio.to_thread(shutil.rmtree, target, True)
            else:
                await asyncio.to_thread(target.unlink, missing_ok=True)

        elif req.operation == "rename":
            new_target = (root / (req.new_path or "")).resolve()
            if not is_safe_path(root, new_target):
                raise HTTPException(403)
            if new_target.name.startswith(".jailrc") or new_target.name.startswith(
                ".jailsh"
            ):
                raise HTTPException(403, "Operation not permitted on this path.")
            await asyncio.to_thread(
                new_target.parent.mkdir, parents=True, exist_ok=True
            )
            await asyncio.to_thread(target.rename, new_target)

        elif req.operation == "copy":
            new_target = (root / (req.new_path or "")).resolve()
            if not is_safe_path(root, new_target):
                raise HTTPException(403)
            if new_target.name.startswith(".jailrc") or new_target.name.startswith(
                ".jailsh"
            ):
                raise HTTPException(403, "Operation not permitted on this path.")
            if target.is_dir():
                await asyncio.to_thread(shutil.copytree, target, new_target)
            else:
                await asyncio.to_thread(
                    new_target.parent.mkdir, parents=True, exist_ok=True
                )
                await asyncio.to_thread(shutil.copy2, target, new_target)

        elif req.operation == "save":
            if req.content is None:
                raise HTTPException(400, "No content provided for save.")
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
            async with aiofiles.open(target, "w", encoding="utf-8") as _f:
                await _f.write(req.content)
        else:
            raise HTTPException(400, f"Unknown operation: {req.operation}")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"File operation failed: {exc}")
    return {"ok": True}


@router.get("/apps/{app_id}/files/download")
async def download_file(
    app_id: int,
    path: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target = (root / path).resolve()
    if not is_safe_path(root, target):
        raise HTTPException(403)
    if not target.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(
        target,
        filename=target.name,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Content-Disposition": f'attachment; filename="{target.name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/apps/{app_id}/files/upload")
async def upload_file_to_path(
    app_id: int,
    path: str = Form(""),
    files: List[UploadFile] = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    root = Path(app_row.app_dir)
    target_dir = (root / path).resolve()
    if not is_safe_path(root, target_dir):
        raise HTTPException(403)
    if not target_dir.is_dir():
        raise HTTPException(404, "Target directory does not exist.")
    _MAX_UPLOAD_BYTES = 50 * 1024 * 1024
    for file in files:
        rel_path = file.filename or "upload"
        if "\0" in rel_path or ".." in rel_path or rel_path.startswith("/"):
            continue

        safe_name = Path(rel_path).name
        if not safe_name or safe_name.startswith(".") or safe_name.startswith(".jail"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.+\-\s()[\]@#!~]+", safe_name) or "\0" in path:
            raise HTTPException(
                400, f"Filename '{safe_name}' contains invalid characters."
            )

        dest = (target_dir / rel_path).resolve()
        if not is_safe_path(root, dest):
            raise HTTPException(403, "Upload path escapes app directory.")

        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)

        data = await file.read(_MAX_UPLOAD_BYTES + 1)
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File '{safe_name}' exceeds 50 MB limit.")
        async with aiofiles.open(dest, "wb") as f:
            await f.write(data)
    return {"ok": True}


@router.get("/apps/{app_id}/packages")
async def list_packages(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)

    if app_row.runtime == "rust":
        manifest_path = Path(app_row.app_dir) / "Cargo.toml"
        if not manifest_path.exists():
            return []
        if not _toml:
            raise HTTPException(500, "tomllib not available (requires Python 3.11+)")
        try:
            with open(manifest_path, "rb") as f:
                data = _toml.load(f)
            deps = data.get("dependencies", {})
            dev_deps = data.get("dev-dependencies", {})

            results = []
            for name, val in {**deps, **dev_deps}.items():
                version = val if isinstance(val, str) else val.get("version", "unknown")
                results.append({"name": name, "version": version})
            return results
        except Exception as e:
            logging.error(f"Failed to parse Cargo.toml: {e}")
            return []

    if app_row.runtime != "python":
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, "Virtual environment python not found.")

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    proc = await asyncio.create_subprocess_exec(
        str(venv_python),
        "-m",
        "pip",
        "list",
        "--format=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    try:
        return json.loads(stdout)
    except Exception:
        return []


@router.post("/apps/{app_id}/packages")
async def install_package(
    app_id: int,
    req: PackageReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)

    pkg_name = req.name.strip()

    if app_row.runtime == "rust":
        manifest_path = Path(app_row.app_dir) / "Cargo.toml"
        proc = await asyncio.create_subprocess_exec(
            "cargo",
            "add",
            pkg_name,
            "--manifest-path",
            str(manifest_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(500, "Cargo add timed out.")
        if proc.returncode != 0:
            raise HTTPException(500, f"Cargo add failed: {stderr.decode()}")
        return {"ok": True}

    if app_row.runtime != "python":
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, "Virtual environment python not found.")
    if not re.fullmatch(
        r"[A-Za-z0-9_.+\-]+(\[[\w,]+\])?([\s]*(==|>=|<=|~=|!=|>|<)[^\s]*)?", pkg_name
    ):
        raise HTTPException(400, "Invalid package name.")

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    proc = await asyncio.create_subprocess_exec(
        str(venv_python),
        "-m",
        "pip",
        "install",
        "--no-input",
        pkg_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, "Install timed out after 5 minutes.")
    if proc.returncode != 0:
        raise HTTPException(500, f"Install failed: {stderr.decode()}")
    return {"ok": True}


@router.delete("/apps/{app_id}/packages/{name}")
async def uninstall_package(
    app_id: int,
    name: str,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)

    if app_row.runtime == "rust":
        manifest_path = Path(app_row.app_dir) / "Cargo.toml"
        proc = await asyncio.create_subprocess_exec(
            "cargo",
            "remove",
            name,
            "--manifest-path",
            str(manifest_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(502, f"Cargo remove failed: {stderr.decode()}")
        return {"ok": True}

    if app_row.runtime != "python":
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not venv_python.exists():
        raise HTTPException(500, "Virtual environment python not found.")
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name):
        raise HTTPException(400, "Invalid package name.")

    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app_row.venv_path)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    proc = await asyncio.create_subprocess_exec(
        str(venv_python),
        "-m",
        "pip",
        "uninstall",
        "-y",
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, "Uninstall timed out.")
    if proc.returncode != 0:
        raise HTTPException(500, f"Uninstall failed: {stderr.decode()}")
    return {"ok": True}


@router.get("/apps/{app_id}/logs")
async def get_app_logs(
    app_id: int,
    lines: int = Query(default=2000, ge=1, le=10000),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Return the last *lines* lines of the app log (default 2000, max 10000)."""
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    log_file = Path(app_row.app_dir) / "logs" / "app.log"
    if not log_file.exists():
        return {"content": ""}

    def _read_tail() -> dict:
        MAX_TAIL_BYTES = 512 * 1024
        try:
            stat = log_file.stat()
            with open(log_file, "rb") as lf:
                if stat.st_size > MAX_TAIL_BYTES:
                    lf.seek(-MAX_TAIL_BYTES, 2)
                    chunk = lf.read().decode("utf-8", errors="replace")
                    chunk = chunk[chunk.find("\n") + 1 :]
                else:
                    chunk = lf.read().decode("utf-8", errors="replace")
            log_lines = chunk.splitlines()
            if len(log_lines) > lines:
                tail = "\n".join(log_lines[-lines:])
                return {
                    "content": tail,
                    "truncated": True,
                    "total_lines": len(log_lines),
                }
            return {"content": chunk, "truncated": False, "total_lines": len(log_lines)}
        except Exception:
            return {"content": ""}

    return await asyncio.to_thread(_read_tail)


@router.delete("/apps/{app_id}/logs")
async def clear_app_logs(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    log_file = Path(app_row.app_dir) / "logs" / "app.log"
    if log_file.exists():
        await asyncio.to_thread(log_file.write_text, "", encoding="utf-8")
    return {"ok": True}


@router.get("/apps/{app_id}/scan-imports")
async def scan_imports(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Scan imported packages to discover dependencies missing from the active venv."""
    import ast as _ast

    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)

    root = Path(app_row.app_dir)
    venv_python = Path(app_row.venv_path) / "bin" / "python"

    installed: Set[str] = set()
    if venv_python.exists():
        env = os.environ.copy()
        env["VIRTUAL_ENV"] = str(app_row.venv_path)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)

        proc = await asyncio.create_subprocess_exec(
            str(venv_python),
            "-m",
            "pip",
            "list",
            "--format=json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            stdout = b"[]"
        try:
            for pkg in json.loads(stdout):
                n = pkg["name"].lower()
                installed.add(n)
                installed.add(n.replace("-", "_"))
                installed.add(n.replace("_", "-"))
        except Exception:
            pass

    stdlib = (
        set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
    )

    imported: Set[str] = set()
    for py_file in root.rglob("*.py"):
        parts = py_file.parts
        if "venv" in parts or "__pycache__" in parts:
            continue
        try:
            text = await asyncio.to_thread(
                py_file.read_text, encoding="utf-8", errors="replace"
            )
            tree = _ast.parse(text)
        except SyntaxError:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                if node.module and node.level == 0:
                    imported.add(node.module.split(".")[0])

    missing = sorted(
        name
        for name in imported
        if name
        and name not in stdlib
        and name not in installed
        and not name.startswith("_")
    )

    return {"missing": missing, "installed_count": len(installed)}


@router.get("/system/info")
async def system_info(_: User = Depends(get_current_user)):
    """Return the server's LAN IPv4 address."""
    ipv4 = await asyncio.to_thread(get_server_ipv4)
    return {"ipv4": ipv4}


@router.get("/system/ports")
async def system_ports(
    _: User = Depends(get_current_user),
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
            (3000, 3010),
            (4000, 4010),
            (5000, 5010),
            (8000, 8020),
            (8080, 8090),
            (8888, 8898),
            (9000, 9010),
            (9090, 9100),
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
                        s.bind(("127.0.0.1", port))
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

    app_port_map: Dict[int, str] = {a.port: a.name for a in app_rows if a.port}
    for entry in result:
        entry["app_name"] = app_port_map.get(entry["port"])

    free_ports = await asyncio.to_thread(_find_free_ports, seen)

    return {"listening": result, "free": free_ports}


@router.get("/system/settings/{key}")
def get_system_setting(
    key: str,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    setting = session.get(SystemSetting, key)
    return {"key": key, "value": setting.value if setting else ""}


def _sanitize_cf_value(v: str) -> str:
    """Strip ALL whitespace and invisible/zero-width Unicode chars."""
    v = v.strip()
    v = "".join(ch for ch in v if ch not in "\r\n\t")
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")
    return v


@router.put("/system/settings/{key}")
def set_system_setting(
    key: str,
    req: SystemSettingReq,
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    value = req.value
    if key.startswith("cf_"):
        value = _sanitize_cf_value(value)
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = value
    else:
        setting = SystemSetting(key=key, value=value)
    session.add(setting)
    session.commit()
    return {"ok": True, "stored_length": len(value)}


class BatchSettingItem(BaseModel):
    key: str
    value: str


@router.post("/system/settings/batch")
def set_system_settings_batch(
    items: List[BatchSettingItem],
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Save multiple settings in a single DB transaction."""
    for item in items:
        value = item.value
        if item.key.startswith("cf_"):
            value = _sanitize_cf_value(value)
        setting = session.get(SystemSetting, item.key)
        if setting:
            setting.value = value
        else:
            setting = SystemSetting(key=item.key, value=value)
        session.add(setting)
    session.commit()
    return {"ok": True, "saved": len(items)}


@router.post("/system/cloudflare/test")
async def test_cf_credentials(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Verify saved Cloudflare integration credentials and permissions."""
    import urllib.error as _ue
    import urllib.request as _ur

    def _fetch(url: str, token: str):
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req = _ur.Request(url, headers=hdrs)
        with _ur.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    creds = _get_cf_creds(session)
    token = creds["cf_api_token"]
    account_id = creds["cf_account_id"]
    tunnel_id = creds["cf_tunnel_id"]

    results = {}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}",
            token,
        )
        if data.get("success"):
            results["account"] = {
                "ok": True,
                "name": data["result"].get("name", account_id),
            }
        else:
            msgs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            results["account"] = {"ok": False, "error": msgs or "Unknown error"}
    except _ue.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            msgs = "; ".join(
                e.get("message", "") for e in json.loads(raw).get("errors", [])
            )
        except Exception:
            msgs = raw[:200]
        results["account"] = {"ok": False, "error": f"HTTP {exc.code}: {msgs}"}
    except Exception as exc:
        results["account"] = {"ok": False, "error": str(exc)}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            token,
        )
        if data.get("success"):
            result = data.get("result") or {}
            actual_id = result.get("id", "")
            tunnel_name = result.get("name", tunnel_id)
            status = result.get("status", "unknown")
            connections = result.get("connections") or []
            active_conns = len(
                [c for c in connections if c.get("is_pending_reconnect") is False]
            )
            results["tunnel"] = {
                "ok": True,
                "name": tunnel_name,
                "id": actual_id,
                "status": status,
                "active_connections": active_conns,
                "message": f"{tunnel_name} ({status}) — {active_conns} active connection(s)",
            }
            if active_conns == 0:
                results["tunnel"]["warning"] = (
                    "No active connections — the cloudflared connector may not be running. "
                    "Start cloudflared on your server or the tunnel will give Error 1016."
                )
        else:
            msgs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            results["tunnel"] = {"ok": False, "error": msgs or "Unknown error"}
    except _ue.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            msgs = "; ".join(
                e.get("message", "") for e in json.loads(raw).get("errors", [])
            )
        except Exception:
            msgs = raw[:200]
        results["tunnel"] = {"ok": False, "error": f"HTTP {exc.code}: {msgs}"}
    except Exception as exc:
        results["tunnel"] = {"ok": False, "error": str(exc)}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/zones?account.id={account_id}&per_page=5",
            token,
        )
        if data.get("success"):
            zone_count = len(data.get("result", []))
            results["dns_zones"] = {
                "ok": True,
                "message": f"Found {zone_count} zone(s) — DNS CNAME records will be managed automatically.",
            }
        else:
            results["dns_zones"] = {
                "ok": False,
                "error": "Cannot list zones — add Zone:Read and DNS:Edit permissions to your API token for automatic DNS management.",
            }
    except _ue.HTTPError as exc:
        results["dns_zones"] = {
            "ok": False,
            "error": f"HTTP {exc.code}: Zone access denied. Add Zone:Read and DNS:Edit to your token.",
        }
    except Exception as exc:
        results["dns_zones"] = {"ok": False, "error": str(exc)}

    all_ok = all(v["ok"] for v in results.values())
    return {"ok": all_ok, "checks": results}


@router.get("/system/cloudflare/zones")
async def list_cf_zones(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Return all active zones (domains) on the Cloudflare account."""
    creds = _get_cf_creds(session)
    # _account_id = creds["cf_account_id"]
    api_token = creds["cf_api_token"]

    zones: List[Dict[str, str]] = []
    page = 1
    per_page = 50

    async def _fetch_zones():
        nonlocal page
        while True:
            url = (
                f"https://api.cloudflare.com/client/v4/zones"
                f"?per_page={per_page}&page={page}&status=active"
            )
            try:
                data = await _cf_request("GET", url, api_token)
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    raise HTTPException(
                        403,
                        "Cloudflare API token lacks Zone:Read permission — cannot list domains. "
                        "Add Zone:Read and DNS:Edit to your token.",
                    )
                raise

            result_list = data.get("result") or []
            for z in result_list:
                name = z.get("name", "")
                zid = z.get("id", "")
                if name and zid:
                    zones.append({"name": name, "id": zid})

            result_info = data.get("result_info") or {}
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages or page >= 10:
                break
            page += 1

    try:
        await asyncio.wait_for(_fetch_zones(), timeout=12.0)
    except asyncio.TimeoutError:
        logging.warning("Cloudflare zone list timed out — returning partial results.")
    except HTTPException:
        raise
    except Exception as exc:
        logging.error(f"CF zone list error: {exc}")
        if not zones:
            raise HTTPException(502, f"Cloudflare API error: {exc}")

    zones.sort(key=lambda z: z["name"])
    return {"ok": True, "zones": zones}


# ---------------------------------------------------------------------------
# Cloudflare Tunnel Route Management
# ---------------------------------------------------------------------------


def _get_cf_creds(session: Session) -> dict:
    """Return sanitized dict with api_token, account_id, tunnel_id or raise 400."""
    keys = ("cf_api_token", "cf_account_id", "cf_tunnel_id")
    result = {}
    for k in keys:
        row = session.get(SystemSetting, k)
        raw = row.value if row else ""
        result[k] = _sanitize_cf_value(raw)
    missing = [k for k, v in result.items() if not v]
    if missing:
        raise HTTPException(
            400, f"Cloudflare not configured. Missing: {', '.join(missing)}"
        )
    tok = result["cf_api_token"]
    logging.info(
        f"CF creds loaded: account={result['cf_account_id']!r} "
        f"tunnel={result['cf_tunnel_id']!r} token_len={len(tok)}"
    )
    return result


@router.get("/apps/{app_id}/tunnel")
def list_cf_routes(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    routes = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all()
    return {
        "routes": [
            {
                "hostname": route.hostname,
                "local_port": route.local_port,
                "published": bool(route.cf_route_id),
                "cf_route_id": route.cf_route_id,
            }
            for route in routes
        ]
    }


@router.get("/apps/{app_id}/tunnel/check")
async def check_cf_hostname(
    app_id: int,
    hostname: str = Query(...),
    local_port: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Pre-flight check before publishing a hostname."""
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)

    clean = (
        hostname.strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .rstrip("/")
    )
    if not clean:
        raise HTTPException(400, "Invalid hostname.")

    existing_route = session.exec(
        select(CfRoute).where(CfRoute.hostname == clean)
    ).first()
    if (
        existing_route
        and existing_route.app_id != app_id
        and existing_route.cf_route_id
    ):
        other_app = session.get(App, existing_route.app_id)
        other_name = other_app.name if other_app else f"app #{existing_route.app_id}"
        return {
            "conflict": True,
            "conflict_type": "local",
            "reason": f"This hostname is already published by '{other_name}'. Publishing here will remove it from that app.",
            "owned_by": other_name,
        }

    try:
        creds = _get_cf_creds(session)
        account_id = creds["cf_account_id"]
        tunnel_id = creds["cf_tunnel_id"]
        api_token = creds["cf_api_token"]

        cfg_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/cfd_tunnel/{tunnel_id}/configurations"
        )
        current_cfg = await _cf_request("GET", cfg_url, api_token)
        result_obj = current_cfg.get("result") or {}
        ingress = result_obj.get("config", {}).get("ingress") or []

        for rule in ingress:
            if rule.get("hostname") == clean:
                current_service = rule.get("service", "unknown")
                server_ip = get_server_ipv4()
                check_port = local_port or app_row.port
                expected_service = (
                    f"http://{server_ip}:{check_port}" if check_port else None
                )
                expected_service_legacy = (
                    f"http://localhost:{check_port}" if check_port else None
                )
                if expected_service and (
                    current_service == expected_service
                    or current_service == expected_service_legacy
                ):
                    return {"conflict": False}
                return {
                    "conflict": True,
                    "conflict_type": "remote",
                    "reason": f"This hostname is already in the tunnel config pointing to '{current_service}'. Publishing will overwrite it.",
                    "owned_by": None,
                }
    except HTTPException:
        pass

    return {"conflict": False}


@router.post("/apps/{app_id}/tunnel/publish")
async def publish_cf_route(
    app_id: int,
    req: CfPublishReq,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Publish (or republish) an app route via Cloudflare Tunnel."""
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)
    if not req.local_port:
        raise HTTPException(
            400, "Local port must be specified for Cloudflare tunnel mapping."
        )

    new_hostname = (
        req.hostname.strip()
        .lower()
        .removeprefix("https://")
        .removeprefix("http://")
        .rstrip("/")
    )
    if not new_hostname or "." not in new_hostname:
        raise HTTPException(
            400,
            "Invalid hostname — must contain at least one dot (e.g. app.example.com).",
        )

    creds = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    tunnel_id = creds["cf_tunnel_id"]
    api_token = creds["cf_api_token"]

    cfg_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/cfd_tunnel/{tunnel_id}/configurations"
    )

    existing_route = session.exec(
        select(CfRoute).where(
            CfRoute.app_id == app_id, CfRoute.local_port == req.local_port
        )
    ).first()
    old_hostname: Optional[str] = (
        existing_route.hostname
        if existing_route and existing_route.cf_route_id and existing_route.hostname
        else None
    )
    is_hostname_change = old_hostname is not None and old_hostname != new_hostname

    current_cfg = await _cf_request("GET", cfg_url, api_token)
    if not current_cfg.get("success"):
        errs = current_cfg.get("errors", [])
        raise HTTPException(502, f"Cloudflare returned failure: {errs}")

    result_obj = current_cfg.get("result") or {}
    ingress = result_obj.get("config", {}).get("ingress") or []
    catch_all = [r for r in ingress if not r.get("hostname")]

    hostnames_to_strip = {new_hostname}
    if is_hostname_change:
        hostnames_to_strip.add(old_hostname)
    named = [
        r
        for r in ingress
        if r.get("hostname") and r["hostname"] not in hostnames_to_strip
    ]

    server_ip = get_server_ipv4()
    named.append(
        {
            "hostname": new_hostname,
            "service": f"http://{server_ip}:{req.local_port}",
        }
    )
    new_ingress = named + (catch_all or [{"service": "http_status:404"}])

    put_resp = await _cf_request(
        "PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}}
    )
    if not put_resp.get("success"):
        errs = put_resp.get("errors", [])
        raise HTTPException(502, f"Cloudflare tunnel update failed: {errs}")

    if is_hostname_change:
        try:
            old_zone_id = await _cf_find_zone_id(old_hostname, account_id, api_token)
            if old_zone_id:
                await _cf_delete_dns_cname(old_zone_id, old_hostname, api_token)
                logging.info(f"Removed old DNS CNAME: {old_hostname}")
        except Exception as exc:
            logging.warning(
                f"Could not delete old DNS CNAME for {old_hostname!r}: {exc}"
            )

    try:
        new_zone_id = await _cf_find_zone_id(new_hostname, account_id, api_token)
    except HTTPException as exc:
        logging.warning(
            f"Zone lookup failed for {new_hostname!r}, rolling back ingress: {exc.detail}"
        )
        try:
            rollback = [
                r
                for r in ingress
                if r.get("hostname") and r["hostname"] not in hostnames_to_strip
            ]
            rollback += catch_all or [{"service": "http_status:404"}]
            await _cf_request(
                "PUT", cfg_url, api_token, {"config": {"ingress": rollback}}
            )
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise

    if not new_zone_id:
        logging.warning(f"Zone not found for {new_hostname!r}, rolling back ingress")
        try:
            rollback = [
                r
                for r in ingress
                if r.get("hostname") and r["hostname"] not in hostnames_to_strip
            ]
            rollback += catch_all or [{"service": "http_status:404"}]
            await _cf_request(
                "PUT", cfg_url, api_token, {"config": {"ingress": rollback}}
            )
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise HTTPException(
            400,
            f"Domain '{new_hostname}' was not found in your Cloudflare account. "
            f"Make sure the root domain is added to Cloudflare and your API token "
            f"has Zone:Read + DNS:Edit permissions.",
        )

    try:
        dns_record_id = await _cf_upsert_dns_cname(
            new_zone_id, new_hostname, tunnel_id, api_token
        )
    except HTTPException as exc:
        logging.warning(
            f"DNS upsert failed for {new_hostname!r}, rolling back ingress: {exc.detail}"
        )
        try:
            rollback = [
                r
                for r in ingress
                if r.get("hostname") and r["hostname"] not in hostnames_to_strip
            ]
            rollback += catch_all or [{"service": "http_status:404"}]
            await _cf_request(
                "PUT", cfg_url, api_token, {"config": {"ingress": rollback}}
            )
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise

    logging.info(
        f"DNS CNAME upserted: {new_hostname} → {tunnel_id}.cfargotunnel.com (record {dns_record_id})"
    )

    conflicting = session.exec(
        select(CfRoute).where(CfRoute.hostname == new_hostname)
    ).all()
    for c in conflicting:
        if c.app_id != app_id:
            other_app = session.get(App, c.app_id)
            if other_app and other_app.external_url == f"https://{new_hostname}":
                other_app.external_url = None
                session.add(other_app)
            c.cf_route_id = None
            c.hostname = ""
            session.add(c)
            logging.info(
                f"Cleared stale CfRoute ownership of {new_hostname!r} from app {c.app_id}"
            )

    route = existing_route or CfRoute(app_id=app_id, local_port=req.local_port)
    route.hostname = new_hostname
    route.cf_route_id = new_hostname
    session.add(route)
    app_row.external_url = f"https://{new_hostname}"
    session.add(app_row)
    session.commit()

    return {
        "ok": True,
        "hostname": new_hostname,
        "url": f"https://{new_hostname}",
        "dns_record_id": dns_record_id,
    }


@router.delete("/apps/{app_id}/tunnel/publish")
async def unpublish_cf_route(
    app_id: int,
    local_port: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Remove public hostname route mapping from Cloudflare tunnel and DNS record."""
    app_row = session.get(App, app_id)
    if not app_row or app_row.user_id != user.id:
        raise HTTPException(404)

    route = session.exec(
        select(CfRoute).where(
            CfRoute.app_id == app_id, CfRoute.local_port == local_port
        )
    ).first()

    if not route or not route.cf_route_id:
        raise HTTPException(400, "No published route found for this app.")

    creds = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    tunnel_id = creds["cf_tunnel_id"]
    api_token = creds["cf_api_token"]

    cfg_url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/cfd_tunnel/{tunnel_id}/configurations"
    )

    hostname = route.hostname

    current_cfg = await _cf_request("GET", cfg_url, api_token)
    if not current_cfg.get("success"):
        errs = current_cfg.get("errors", [])
        raise HTTPException(
            502, f"Cloudflare returned failure fetching tunnel config: {errs}"
        )

    result_obj = current_cfg.get("result") or {}
    ingress = result_obj.get("config", {}).get("ingress") or []
    named = [r for r in ingress if r.get("hostname") and r["hostname"] != hostname]
    catch_all = [r for r in ingress if not r.get("hostname")]
    new_ingress = named + (catch_all or [{"service": "http_status:404"}])

    put_resp = await _cf_request(
        "PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}}
    )
    if not put_resp.get("success"):
        raise HTTPException(
            502, f"Cloudflare tunnel update failed: {put_resp.get('errors')}"
        )

    try:
        zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
        if zone_id:
            await _cf_delete_dns_cname(zone_id, hostname, api_token)
            logging.info(f"DNS CNAME deleted for {hostname}")
        else:
            logging.warning(
                f"Zone not found for {hostname!r} during unpublish — DNS record may remain."
            )
    except Exception as exc:
        logging.warning(f"Could not delete DNS record for {hostname}: {exc}")

    route.cf_route_id = None
    route.hostname = ""
    session.add(route)

    other_active = session.exec(
        select(CfRoute).where(CfRoute.app_id == app_id, CfRoute.cf_route_id != None)
    ).first()

    if not other_active:
        app_row.external_url = None
        session.add(app_row)

    session.commit()
    return {"ok": True}


@router.websocket("/apps/{app_id}/terminal")
async def terminal_websocket(
    websocket: WebSocket,
    app_id: int,
    session: Session = Depends(get_session),
):
    await websocket.accept()

    token = websocket.cookies.get("session")
    if not token:
        await websocket.close(code=1008, reason="Not authenticated.")
        return

    db_session = session.get(UserSession, token)
    if not db_session:
        await websocket.close(code=1008, reason="Session invalid.")
        return

    if utc_now() > _make_tz_aware(db_session.expires_at):
        session.delete(db_session)
        session.commit()
        await websocket.close(code=1008, reason="Session expired.")
        return

    try:
        await terminal_manager.start_terminal(
            websocket, app_id, session, db_session.user_id
        )
    except Exception as exc:
        logging.error(f"WS terminal error: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass
