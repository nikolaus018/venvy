"""App routes: list, create, control, settings, delete, stats, packages, logs."""
import asyncio
import json
import logging
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlmodel import select, Session

from venvy.apps import _app_safe, _clean_app_name, sanitize_slug
from venvy.audit import audit_log
from venvy.auth import check_ip_allowlist, get_current_user, rate_limit_mutations, require_operator
from venvy.cloudflare import _cf_delete_dns_cname, _cf_find_zone_id, _cf_request, _get_cf_creds
from venvy.config import (
    APP_LOGS_DIR,
    APPS_BASE_DIR,
    _make_tz_aware,
    TERMINAL_RC_DIR,
    _toml,
    utc_now,
)
from venvy.crypto import _encrypt_secret
from venvy.db import engine, get_session
from venvy.isolation import (
    _app_ids,
    _app_log_file,
    _app_proc_kwargs,
    _app_tool_env,
    _delete_app_user,
    _ensure_app_user,
    _isolation_active,
)
from venvy.jobs import _running_tasks, _start_job
from venvy.models import App, CfRoute, User
from venvy.process import (
    _app_lock,
    _app_locks,
    _app_metrics,
    _app_pid_alive,
    _cancel_log_task,
    _restart_backoff,
    restore_in_progress,
    _start_app_process,
    start_app_process_logic,
    _stop_app_process,
    stop_app_process_logic,
    _trigger_supervisor,
)
from venvy.safefs import AppPathError, _fs_open_file, _fs_save, is_safe_path, _upload_rel_path_error
from venvy.uploads import _save_upload_file, _SINGLE_REQUEST_UPLOAD_MAX

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist)]
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AppUpdateReq(BaseModel):
    name: Optional[str] = None
    start_on_boot: Optional[bool] = None
    restart_policy: Optional[str] = None
    runtime: Optional[str] = None
    port: Optional[int] = None
    manual_ports: Optional[List[int]] = None
    env_vars: Optional[Dict[str, str]] = None
    entrypoint: Optional[str] = None
    url_source_pref: Optional[str] = None
    memory_limit_mb: Optional[int] = None
    cpu_limit_percent: Optional[int] = None


class PackageReq(BaseModel):
    name: str


# ---------------------------------------------------------------------------
# App endpoints
# ---------------------------------------------------------------------------

@router.get("/apps")
def list_apps(
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    # Apps belong to the instance, not to the logged-in user — every authenticated
    # user sees every app (viewers are read-only; enforcement happens on the
    # mutating routes below via require_operator).
    apps = session.exec(select(App).order_by(App.name)).all()
    changed = False
    for app_row in apps:
        if app_row.status in ("starting", "stopping"):
            continue
        if app_row.status == "running" and app_row.pid:
            if not _app_pid_alive(app_row):
                app_row.status = "stopped"
                app_row.pid = None
                app_row.pid_start = None
                app_row.last_stop_time = utc_now()
                session.add(app_row)
                changed = True
        elif app_row.status == "stopped" and app_row.pid:
            app_row.pid = None
            session.add(app_row)
            changed = True
    if changed:
        session.commit()
    can_see_secrets = user.role in ("admin", "operator")
    return [_app_safe(a, reveal_secrets=can_see_secrets) for a in apps]


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
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    if runtime not in ("python", "rust"):
        raise HTTPException(400, "runtime must be one of: python, rust")
    if restart_policy not in ("never", "on-failure", "always"):
        raise HTTPException(400, "restart_policy must be one of: never, on-failure, always")
    name = _clean_app_name(name)

    entrypoint_clean = ""
    if runtime == "rust":
        entrypoint_clean = "main.rs"
    elif entrypoint.strip():
        entrypoint_strip = entrypoint.strip()
        entrypoint_clean = Path(entrypoint_strip).name
        if not entrypoint_clean or entrypoint_clean != entrypoint_strip:
            raise HTTPException(400, "Entrypoint must be a plain filename (e.g. main.py), not a path.")
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", entrypoint_clean):
            raise HTTPException(400, "Entrypoint filename contains invalid characters.")

    slug = sanitize_slug(name)
    user_apps_dir = APPS_BASE_DIR / str(user.id)
    await asyncio.to_thread(user_apps_dir.mkdir, exist_ok=True)

    app_dir = user_apps_dir / slug
    if await asyncio.to_thread(app_dir.exists):
        slug = f"{slug}-{int(time.time())}"
        app_dir = user_apps_dir / slug
    await asyncio.to_thread(app_dir.mkdir, parents=True, mode=0o750)
    if not _isolation_active():
        await asyncio.to_thread((app_dir / "logs").mkdir, mode=0o750)

    skipped_files: List[Dict[str, str]] = []
    for file in (files or []):
        rel_path = file.filename or "upload"
        reason = _upload_rel_path_error(rel_path)
        dest = (app_dir / rel_path).resolve()
        if not reason and not is_safe_path(app_dir, dest):
            reason = "path escapes app directory"
        if reason:
            skipped_files.append({"path": rel_path, "reason": reason})
            continue
        await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
        if not await asyncio.to_thread(_save_upload_file, file, dest, _SINGLE_REQUEST_UPLOAD_MAX):
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(413, f"File '{rel_path}' exceeds the {_SINGLE_REQUEST_UPLOAD_MAX // (1024 * 1024)} MB single-request limit.")
    req_bytes = await requirements.read() if requirements and requirements.filename else None

    venv_path = app_dir / "venv"
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
        status="installing",
    )
    session.add(new_app)
    session.commit()
    session.refresh(new_app)
    app_id = new_app.id
    audit_log(user.username, "app_create", f"created app '{new_app.name}' (id={app_id})")

    async def _finish() -> dict:
        """venv + pip install, which can take minutes — runs as a job."""
        try:
            with Session(engine) as s2:
                row = s2.get(App, app_id)
            await asyncio.to_thread(_ensure_app_user, row)
            kw = _app_proc_kwargs(row)
            env = _app_tool_env(row)
            if runtime == "python":
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-m", "venv", str(venv_path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **kw,
                )
                try:
                    _, venv_err = await asyncio.wait_for(proc.communicate(), timeout=120)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise HTTPException(500, "venv creation timed out.")
                if proc.returncode != 0:
                    err_msg = venv_err.decode(errors="replace")
                    hint = ""
                    if "ensurepip" in err_msg or "venv" in err_msg.lower():
                        hint = f" Hint: python3-venv may not be installed — run: sudo apt-get install -y python3.{sys.version_info.minor}-venv"
                    raise HTTPException(500, f"venv creation failed: {err_msg}{hint}")

            if req_bytes is not None and runtime == "python":
                req_path = app_dir / "requirements.txt"
                await asyncio.to_thread(_fs_save, app_dir, ("requirements.txt",), req_bytes, _app_ids(row))
                pip_bin = venv_path / "bin" / "pip"
                if not await asyncio.to_thread(pip_bin.exists):
                    raise HTTPException(500, "pip not found in the new virtual environment.")
                proc = await asyncio.create_subprocess_exec(
                    str(pip_bin), "install", "--no-input", "-r", str(req_path),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, **kw,
                )
                try:
                    _, pip_err = await asyncio.wait_for(proc.communicate(), timeout=600)
                except asyncio.TimeoutError:
                    proc.kill()
                    raise HTTPException(500, "pip install timed out after 10 minutes.")
                if proc.returncode != 0:
                    raise HTTPException(500, f"pip install failed: {pip_err.decode(errors='replace')}")

            with Session(engine) as s2:
                row = s2.get(App, app_id)
                row.status = "stopped"
                s2.add(row)
                s2.commit()
                s2.refresh(row)
                result = _app_safe(row)
            result["skipped_files"] = skipped_files
            return result
        except BaseException:
            # Failed or cancelled: remove the half-created app entirely.
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            with Session(engine) as s2:
                row = s2.get(App, app_id)
                if row:
                    s2.delete(row)
                    s2.commit()
            await asyncio.to_thread(_delete_app_user, app_id)
            raise

    return {**_start_job(user, "app_create", _finish), "app_id": app_id, "skipped_files": skipped_files}


@router.post("/apps/{app_id}/control")
async def control_app(
    app_id: int,
    action: str = Query(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "Invalid action. Use: start | stop | restart")
    if app_row.status == "installing":
        raise HTTPException(409, "This app is still being set up — wait for its install to finish.")
    if restore_in_progress() and action != "stop":
        raise HTTPException(409, "A restore is in progress — apps can be started again once Venvy has restarted.")
    audit_log(user.username, f"app_{action}", f"app '{app_row.name}' (id={app_id})")

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
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a and a.status == "stopping":
                    a.status = "running" if a.pid else "stopped"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_restart():
        try:
            # One lock hold for both halves, so nothing else can start the
            # app in the gap between them.
            async with _app_lock(app_id):
                with Session(engine) as s:
                    await _stop_app_process(app_id, s, manual=False)
                with Session(engine) as s:
                    await _start_app_process(app_id, s)
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

    _restart_backoff.pop(app_id, None)
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
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if req.name is not None:
        app_row.name = _clean_app_name(req.name)
    if req.start_on_boot is not None:
        app_row.start_on_boot = req.start_on_boot
    if req.restart_policy is not None:
        if req.restart_policy not in ("never", "on-failure", "always"):
            raise HTTPException(400, "restart_policy must be one of: never, on-failure, always")
        app_row.restart_policy = req.restart_policy
    if req.port is not None:
        if req.port != 0 and not (1 <= req.port <= 65535):
            raise HTTPException(400, "Port must be between 1 and 65535 (or 0 to clear).")
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
        for env_key, env_value in req.env_vars.items():
            # Anything else can't be put in a process environment at all, and
            # only failed later, as an unexplained error on start.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
                raise HTTPException(400, f"Invalid environment variable name: {env_key!r}")
            if "\x00" in env_value:
                raise HTTPException(400, f"Environment variable {env_key} contains a NUL character.")
        app_row.env_json = _encrypt_secret(json.dumps(req.env_vars))
        audit_log(user.username, "app_env_change", f"app '{app_row.name}' (id={app_id})")
    # Runtime first: the entrypoint rules below depend on it, and a request
    # that switches a Rust app to Python *and* names its entrypoint used to
    # have that entrypoint overwritten with main.rs.
    if req.runtime is not None:
        if req.runtime not in ("python", "rust"):
            raise HTTPException(400, "runtime must be one of: python, rust")
        app_row.runtime = req.runtime
        if app_row.runtime == "rust":
            app_row.entrypoint = "main.rs"
    if req.entrypoint is not None:
        if app_row.runtime == "rust":
            app_row.entrypoint = "main.rs"
        else:
            ep_strip = req.entrypoint.strip()
            if ep_strip == "":
                app_row.entrypoint = ""
            else:
                ep_clean = Path(ep_strip).name
                if not ep_clean or ep_clean != ep_strip:
                    raise HTTPException(400, "Entrypoint must be a plain filename (e.g. main.py).")
                if not re.fullmatch(r"[A-Za-z0-9_.\-]+", ep_clean):
                    raise HTTPException(400, "Entrypoint filename contains invalid characters.")
                app_row.entrypoint = ep_clean
    if req.url_source_pref is not None:
        if req.url_source_pref not in ("local", "public"):
            raise HTTPException(400, "url_source_pref must be 'local' or 'public'")
        app_row.url_source_pref = req.url_source_pref
    if req.memory_limit_mb is not None:
        if req.memory_limit_mb != 0 and not (16 <= req.memory_limit_mb <= 1_048_576):
            raise HTTPException(400, "memory_limit_mb must be between 16 and 1048576 (or 0 for unlimited).")
        app_row.memory_limit_mb = None if req.memory_limit_mb == 0 else req.memory_limit_mb
    if req.cpu_limit_percent is not None:
        if req.cpu_limit_percent != 0 and not (1 <= req.cpu_limit_percent <= 10000):
            raise HTTPException(400, "cpu_limit_percent must be between 1 and 10000 (or 0 for unlimited).")
        app_row.cpu_limit_percent = None if req.cpu_limit_percent == 0 else req.cpu_limit_percent

    app_row.updated_at = utc_now()
    session.add(app_row)
    session.commit()
    refreshed = session.get(App, app_id)
    return _app_safe(refreshed)


@router.delete("/apps/{app_id}")
async def delete_app(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if app_row.status == "installing":
        raise HTTPException(409, "This app is still being set up — wait for its install to finish.")
    app_name = app_row.name
    _cancel_log_task(app_id)
    await stop_app_process_logic(app_id, session)

    # An app has one route per published port. Every one of them has to go:
    # a hostname left in the tunnel keeps forwarding to that port, where the
    # next app to bind it would be exposed to the internet unasked.
    cf_routes = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all()
    published = {r.hostname for r in cf_routes if r.cf_route_id and r.hostname}
    if published:
        try:
            creds      = _get_cf_creds(session)
            account_id = creds["cf_account_id"]
            tunnel_id  = creds["cf_tunnel_id"]
            api_token  = creds["cf_api_token"]
            cfg_url = (
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
                f"/cfd_tunnel/{tunnel_id}/configurations"
            )
            current_cfg = await _cf_request("GET", cfg_url, api_token)
            result_obj  = current_cfg.get("result") or {}
            ingress     = result_obj.get("config", {}).get("ingress") or []
            named       = [r for r in ingress if r.get("hostname") and r["hostname"] not in published]
            catch_all   = [r for r in ingress if not r.get("hostname")]
            new_ingress = named + (catch_all or [{"service": "http_status:404"}])
            await _cf_request("PUT", cfg_url, api_token, {"config": {"ingress": new_ingress}})
            for hostname in sorted(published):
                try:
                    zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
                    if zone_id:
                        await _cf_delete_dns_cname(zone_id, hostname, api_token)
                    logging.info(f"CF route cleaned up for deleted app {app_id} ({hostname})")
                except Exception as exc:
                    logging.warning(f"CF DNS cleanup failed for app {app_id} ({hostname}): {exc}")
        except Exception as exc:
            logging.warning(f"CF cleanup failed for app {app_id} during delete: {exc}")
    for cf_route in cf_routes:
        session.delete(cf_route)

    app_dir_path = Path(app_row.app_dir)
    if await asyncio.to_thread(app_dir_path.exists):
        await asyncio.to_thread(shutil.rmtree, app_row.app_dir, ignore_errors=True)
    await asyncio.to_thread(shutil.rmtree, APP_LOGS_DIR / str(app_id), True)
    (TERMINAL_RC_DIR / f"app-{app_id}.bashrc").unlink(missing_ok=True)
    await asyncio.to_thread(_delete_app_user, app_id)
    _restart_backoff.pop(app_id, None)
    session.delete(app_row)
    session.commit()
    _app_locks.pop(app_id, None)
    audit_log(user.username, "app_delete", f"deleted app '{app_name}' (id={app_id})")
    return {"ok": True}


@router.get("/apps/{app_id}/stats")
def app_stats(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    stats: Dict = {"cpu": 0.0, "memory": 0.0, "uptime": 0.0}
    if _app_pid_alive(app_row):
        # From the metrics collector: cpu_percent() on a freshly created
        # psutil.Process always reads 0.0, which is all this used to return,
        # and the collector's figures cover the app's child processes too.
        metrics = _app_metrics.get(app_id)
        if metrics:
            stats["cpu"] = metrics["cpu"]
            stats["memory"] = metrics["memory"] / (1024 * 1024)
        if app_row.last_start_time:
            stats["uptime"] = (utc_now() - _make_tz_aware(app_row.last_start_time)).total_seconds()
    return stats


@router.get("/apps/{app_id}/packages")
async def list_packages(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    
    if app_row.runtime == "rust":
        try:
            manifest, _ = _fs_open_file(Path(app_row.app_dir), ("Cargo.toml",))
        except AppPathError:
            return []
        if not _toml:
            manifest.close()
            raise HTTPException(500, "tomllib not available (requires Python 3.11+)")
        try:
            with manifest as f:
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
    
    # The venv's python runs app-controlled code (.pth files, sitecustomize),
    # so it runs as the app's user, never as Venvy's.
    proc = await asyncio.create_subprocess_exec(
        str(venv_python), "-m", "pip", "list", "--format=json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_app_tool_env(app_row),
        **_app_proc_kwargs(app_row),
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


async def _run_app_tool(app_row: App, argv: List[str], timeout: int, what: str) -> None:
    """Run a package-manager command for an app (as the app's user); raise
    HTTPException with its stderr on failure or timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_app_tool_env(app_row),
        **_app_proc_kwargs(app_row),
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, f"{what} timed out after {timeout} seconds.")
    if proc.returncode != 0:
        raise HTTPException(500, f"{what} failed: {stderr.decode(errors='replace')}")


@router.post("/apps/{app_id}/packages")
async def install_package(
    app_id: int,
    req: PackageReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Install a package. Answers {"job_id"} — poll GET /api/jobs/{id}."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    pkg_name = req.name.strip()

    if app_row.runtime == "rust":
        if not re.fullmatch(r"[A-Za-z0-9_.+\-]+(@[A-Za-z0-9_.+\-]+)?", pkg_name) or pkg_name.startswith("-"):
            raise HTTPException(400, "Invalid package name.")
        argv = ["cargo", "add", pkg_name, "--manifest-path", str(Path(app_row.app_dir) / "Cargo.toml")]
        timeout, what = 300, "cargo add"
    elif app_row.runtime == "python":
        venv_python = Path(app_row.venv_path) / "bin" / "python"
        if not venv_python.exists():
            raise HTTPException(500, "Virtual environment python not found.")
        # No leading "-": the name must never be read as a pip option.
        if pkg_name.startswith("-") or not re.fullmatch(
                r"[A-Za-z0-9_.+\-]+(\[[\w,]+\])?([\s]*(==|>=|<=|~=|!=|>|<)[^\s]*)?", pkg_name):
            raise HTTPException(400, "Invalid package name.")
        argv = [str(venv_python), "-m", "pip", "install", "--no-input", pkg_name]
        timeout, what = 600, "pip install"
    else:
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    async def _job() -> dict:
        await _run_app_tool(app_row, argv, timeout, what)
        audit_log(user.username, "package_install", f"app '{app_row.name}' (id={app_id}): {pkg_name}")
        return {"ok": True}

    return _start_job(user, "package_install", _job)


@router.delete("/apps/{app_id}/packages/{name}")
async def uninstall_package(
    app_id: int,
    name: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Uninstall a package. Answers {"job_id"} — poll GET /api/jobs/{id}."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name) or name.startswith("-"):
        raise HTTPException(400, "Invalid package name.")

    if app_row.runtime == "rust":
        argv = ["cargo", "remove", name, "--manifest-path", str(Path(app_row.app_dir) / "Cargo.toml")]
        timeout, what = 120, "cargo remove"
    elif app_row.runtime == "python":
        venv_python = Path(app_row.venv_path) / "bin" / "python"
        if not venv_python.exists():
            raise HTTPException(500, "Virtual environment python not found.")
        argv = [str(venv_python), "-m", "pip", "uninstall", "-y", name]
        timeout, what = 300, "pip uninstall"
    else:
        raise HTTPException(400, "Packages tab is only supported for Python/Rust.")

    async def _job() -> dict:
        await _run_app_tool(app_row, argv, timeout, what)
        audit_log(user.username, "package_uninstall", f"app '{app_row.name}' (id={app_id}): {name}")
        return {"ok": True}

    return _start_job(user, "package_uninstall", _job)


@router.get("/apps/{app_id}/logs")
async def get_app_logs(
    app_id: int,
    lines: int = Query(default=2000, ge=1, le=10000),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """Return the last *lines* lines of the app log (default 2000, max 10000)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    log_file = _app_log_file(app_row)
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
                    chunk = chunk[chunk.find("\n") + 1:]
                else:
                    chunk = lf.read().decode("utf-8", errors="replace")
            log_lines = chunk.splitlines()
            if len(log_lines) > lines:
                tail = "\n".join(log_lines[-lines:])
                return {"content": tail, "truncated": True, "total_lines": len(log_lines)}
            return {"content": chunk, "truncated": False, "total_lines": len(log_lines)}
        except Exception:
            return {"content": ""}

    return await asyncio.to_thread(_read_tail)


@router.delete("/apps/{app_id}/logs")
async def clear_app_logs(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    log_file = _app_log_file(app_row)
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
    if not app_row:
        raise HTTPException(404)

    root = Path(app_row.app_dir)
    venv_python = Path(app_row.venv_path) / "bin" / "python"

    installed: Set[str] = set()
    if venv_python.exists():
        proc = await asyncio.create_subprocess_exec(
            str(venv_python), "-m", "pip", "list", "--format=json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_app_tool_env(app_row),
            **_app_proc_kwargs(app_row),
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

    stdlib = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()

    imported: Set[str] = set()
    for py_file in root.rglob("*.py"):
        parts = py_file.parts
        if "venv" in parts or "__pycache__" in parts:
            continue
        try:
            fileobj, _st = await asyncio.to_thread(_fs_open_file, root, py_file.relative_to(root).parts)
            with fileobj:
                text = (await asyncio.to_thread(fileobj.read)).decode("utf-8", errors="replace")
            tree = _ast.parse(text)
        except (AppPathError, OSError, SyntaxError, ValueError, RecursionError):
            # Not a plain file we can reach without following a link, or not
            # parseable Python (binary content raises ValueError, not SyntaxError).
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                if node.module and node.level == 0:
                    imported.add(node.module.split(".")[0])

    missing = sorted(
        name for name in imported
        if name
        and name not in stdlib
        and name not in installed
        and not name.startswith("_")
    )

    return {"missing": missing, "installed_count": len(installed)}
