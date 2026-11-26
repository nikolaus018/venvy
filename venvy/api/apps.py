"""App routes: list, create, control, settings, delete, stats, packages, logs."""
import ast
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel
from sqlmodel import delete as sql_delete, select, Session

from venvy import deploy, monitor, own_hosts, proxy, runtimes
from venvy.api.cloudflare import _check_publish_port
from venvy.apps import _app_safe, _clean_app_name, sanitize_slug
from venvy.audit import audit_log
from venvy.auth import (
    check_ip_allowlist, get_current_user, has_app_access, rate_limit_mutations, require_app_access,
    require_operator, visible_app_ids,
)
from venvy.cloudflare import _cf_delete_dns_cname, _cf_find_zone_id, _cf_update_ingress, _get_cf_creds
from venvy.config import (
    APP_LOGS_DIR,
    APPS_BASE_DIR,
    _make_tz_aware,
    TERMINAL_RC_DIR,
    _toml,
    utc_now,
)
from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine, get_session
from venvy.isolation import (
    _app_ids,
    _app_log_file,
    _as_app_user,
    _app_tool_env,
    _delete_app_user,
    _ensure_app_user,
    _isolation_active,
    is_dangerous_env_var,
)
from venvy.jobs import _running_tasks, _start_job
from venvy.models import (
    AlertRule, App, AppAccess, CfRoute, EnvHistory, MetricSample, Release, ScheduledJob, User,
)
from venvy.process import (
    _active_app_pids,
    _app_lock,
    _app_locks,
    _app_metrics,
    _app_pid_alive,
    _cancel_log_task,
    forget_app_runtime,
    restart_app_process_logic,
    _restart_backoff,
    restore_in_progress,
    start_app_process_logic,
    _stop_app_process,
    stop_app_process_logic,
    _trigger_supervisor,
)
from venvy.safefs import AppPathError, _fs_open_file, _fs_save, is_safe_path, _upload_rel_path_error
from venvy.terminal import terminal_manager
from venvy.uploads import _save_upload_file, _SINGLE_REQUEST_UPLOAD_MAX

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)]
)

_REQUIREMENTS_MAX_BYTES = 1024 * 1024


# Where venv/pip/cargo/npm run from. Not Venvy's own working directory: under
# per-app users the app's user can't enter it, and `python -m …` would put
# whatever directory it starts in on its import path.
_TOOL_CWD = "/"


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
    healthcheck_path: Optional[str] = None      # "" switches the check off
    health_interval: Optional[int] = None
    health_failures: Optional[int] = None
    health_restart: Optional[bool] = None
    domain: Optional[str] = None                # "" removes it
    disk_limit_mb: Optional[int] = None         # 0 removes it
    # Run, build, deploy, network and sandbox settings (see apply_spec_fields)
    start_command: Optional[str] = None         # "" clears
    install_command: Optional[str] = None
    release_command: Optional[str] = None
    root_dir: Optional[str] = None
    stop_signal: Optional[str] = None
    stop_timeout: Optional[int] = None          # 0 = server default
    container_port: Optional[int] = None
    health_type: Optional[str] = None
    health_command: Optional[str] = None
    health_start_period: Optional[int] = None
    instances: Optional[int] = None
    zero_downtime: Optional[bool] = None
    deploy_strategy: Optional[str] = None
    spec_from_repo: Optional[bool] = None
    commit_status: Optional[bool] = None
    previews_enabled: Optional[bool] = None
    preview_domain: Optional[str] = None
    preview_env: Optional[Dict[str, str]] = None
    environment: Optional[str] = None
    bind_public: Optional[bool] = None
    egress_policy: Optional[str] = None
    egress_allow: Optional[str] = None
    sandbox: Optional[bool] = None
    pids_limit: Optional[int] = None
    io_weight: Optional[int] = None
    nofile_limit: Optional[int] = None
    access_log: Optional[bool] = None
    sops_file: Optional[str] = None
    vault_path: Optional[str] = None
    disk_limit_action: Optional[str] = None
    runtime_version: Optional[str] = None       # "" = the host's own tool
    secret_files: Optional[Dict[str, str]] = None


_HOST_RE = re.compile(r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def _clean_domain(value: str) -> Optional[str]:
    d = (value or "").strip().lower().rstrip(".")
    if not d:
        return None
    if len(d) > 253 or not _HOST_RE.match(d):
        raise HTTPException(400, "The domain must be a hostname like app.example.com.")
    return d


class PackageReq(BaseModel):
    name: str


_SIGNAL_NAMES = ("SIGTERM", "SIGINT", "SIGQUIT", "SIGHUP", "SIGUSR1", "SIGUSR2", "SIGWINCH")
_CIDR_RE = re.compile(r"[0-9A-Fa-f:.]+(/\d{1,3})?")


def _int_in(name: str, v, lo: int, hi: int, zero_none: bool = False):
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number.")
    if zero_none and v == 0:
        return None
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}" + (" (or 0 for none)." if zero_none else "."))
    return v


def _bool(name: str, v) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError(f"{name} must be true or false.")


def apply_spec_fields(row: App, fields: Dict[str, object], admin: bool = False) -> List[str]:
    """Validate and set app settings by field name — from the API (PUT
    /apps/{id}) and from a repository's venvy.toml. Raises ValueError with a
    message for the user. Returns the names of the fields that changed.
    *admin*: the change comes from an admin (only one may pick a runtime that
    runs as root, see runtimes.ROOT_RUNTIMES); a venvy.toml never is."""
    from venvy import appspec, secrets_ext
    changed: List[str] = []

    def put(name: str, value) -> None:
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed.append(name)

    if "runtime" in fields:
        rt = fields["runtime"]
        if rt not in runtimes.RUNTIMES:
            raise ValueError(runtimes.RUNTIME_ERROR)
        if rt != row.runtime:
            if rt in runtimes.ROOT_RUNTIMES and not admin:
                raise ValueError(runtimes.ROOT_RUNTIME_ERROR)
            old = runtimes.RUNTIMES.get(row.runtime)
            if old is not None and old.entry == runtimes.FIXED:
                row.entrypoint = ""
            put("runtime", rt)
            if row.runtime in ("rust", "go") and row.entrypoint in ("main.rs", "main.go"):
                row.entrypoint = ""
        if runtimes.RUNTIMES[row.runtime].entry == runtimes.FIXED:
            row.entrypoint = runtimes.RUNTIMES[row.runtime].entry_default
    for key, value in fields.items():
        if value is None:
            continue
        if key == "runtime":
            continue
        if key == "entrypoint":
            put("entrypoint", runtimes.clean_entrypoint(row.runtime, str(value)))
        elif key == "runtime_version":
            put("runtime_version", runtimes.clean_runtime_version(row.runtime, str(value)))
        elif key == "root_dir":
            put("root_dir", appspec.clean_rel_path(str(value), "root folder"))
        elif key in ("start_command", "install_command", "release_command", "build_command", "health_command"):
            what = key.replace("_", " ")
            if key == "build_command":
                cleaned = deploy.clean_build_command(str(value))
            else:
                cleaned = appspec.clean_command(str(value), what)
            put(key, cleaned)
        elif key == "stop_signal":
            sig = str(value).upper()
            sig = sig if sig.startswith("SIG") else "SIG" + sig
            if sig not in _SIGNAL_NAMES:
                raise ValueError("stop_signal must be one of " + ", ".join(_SIGNAL_NAMES))
            put("stop_signal", sig)
        elif key == "stop_timeout":
            put("stop_timeout", _int_in("stop_timeout", value, 1, 3600, zero_none=True))
        elif key == "container_port":
            put("container_port", _int_in("container_port", value, 1, 65535, zero_none=True))
        elif key == "health_type":
            if value not in ("http", "tcp", "command", "none"):
                raise ValueError("health_type must be http, tcp, command or none.")
            put("health_type", value)
        elif key == "healthcheck_path":
            put("healthcheck_path", monitor.normalize_path(str(value)))
        elif key == "health_interval":
            put("health_interval", _int_in("health_interval", value, 5, 3600))
        elif key == "health_failures":
            put("health_failures", _int_in("health_failures", value, 1, 20))
        elif key == "health_start_period":
            put("health_start_period", _int_in("health_start_period", value, 0, 3600))
        elif key == "instances":
            put("instances", _int_in("instances", value, 1, 32))
        elif key in ("zero_downtime", "spec_from_repo", "commit_status", "previews_enabled", "bind_public",
                     "sandbox", "access_log"):
            put(key, _bool(key, value))
        elif key == "deploy_strategy":
            if value not in ("in_place", "atomic"):
                raise ValueError("deploy_strategy must be in_place or atomic.")
            put(key, value)
        elif key == "preview_domain":
            put(key, _clean_domain(str(value)) if value else None)
        elif key == "environment":
            v = str(value).strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9\-]{0,30}", v):
                raise ValueError("environment must be a short name like production or staging.")
            put(key, v)
        elif key == "egress_policy":
            if value not in ("allow", "deny", "allowlist"):
                raise ValueError("egress_policy must be allow, deny or allowlist.")
            put(key, value)
        elif key == "egress_allow":
            items = [c.strip() for c in str(value).split(",") if c.strip()]
            for c in items:
                if not _CIDR_RE.fullmatch(c):
                    raise ValueError(f"'{c}' is not an IP address or CIDR range.")
                import ipaddress
                try:
                    ipaddress.ip_network(c, strict=False)
                except ValueError:
                    raise ValueError(f"'{c}' is not an IP address or CIDR range.")
            put(key, ",".join(items))
        elif key == "pids_limit":
            put(key, _int_in(key, value, 16, 1_000_000, zero_none=True))
        elif key == "io_weight":
            put(key, _int_in(key, value, 1, 10000, zero_none=True))
        elif key == "nofile_limit":
            put(key, _int_in(key, value, 64, 1_048_576, zero_none=True))
        elif key == "disk_limit_action":
            if value not in ("alert", "stop"):
                raise ValueError("disk_limit_action must be alert or stop.")
            put(key, value)
        elif key == "sops_file":
            put(key, appspec.clean_rel_path(str(value), "SOPS file") or None)
        elif key == "vault_path":
            put(key, secrets_ext.clean_vault_path(str(value)))
        else:
            raise ValueError(f"'{key}' can't be set.")
    return changed


# Settings the API takes by name and hands to apply_spec_fields().
SPEC_FIELDS = (
    "start_command", "install_command", "release_command", "root_dir", "stop_signal", "stop_timeout",
    "container_port", "health_type", "health_command", "health_start_period", "instances", "zero_downtime",
    "deploy_strategy", "spec_from_repo", "commit_status", "previews_enabled", "preview_domain", "environment",
    "bind_public", "egress_policy", "egress_allow", "sandbox", "pids_limit", "io_weight", "nofile_limit",
    "access_log", "sops_file", "vault_path", "disk_limit_action", "runtime_version",
)




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
    allowed = visible_app_ids(session, user)       # None: all of them
    if allowed is not None:
        apps = [a for a in apps if a.id in allowed]
    changed = False
    for app_row in apps:
        if app_row.status in ("starting", "stopping"):
            continue
        if app_row.status == "running" and app_row.pid:
            # Skip processes actively managed in this Venvy run — _reap_process
            # owns their transition so exit codes and crash notifications aren't lost.
            if _active_app_pids.get(app_row.id) == app_row.pid:
                continue
            if not _app_pid_alive(app_row):
                app_row.status = "stopped"
                app_row.pid = None
                app_row.pid_start = None
                app_row.systemd_scope = None
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


async def _make_venv(row: App, version: Optional[str]) -> None:
    """Create the app's virtualenv with the Python of *version* (None: the system's python3)."""
    py = runtimes.python_binary(version)
    proc = await asyncio.create_subprocess_exec(
        *_as_app_user(row, [py, "-m", "venv", str(row.venv_path)]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_app_tool_env(row),
        cwd=_TOOL_CWD, start_new_session=True,
    )
    try:
        _, venv_err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise HTTPException(500, "venv creation timed out.")
    if proc.returncode != 0:
        err_msg = venv_err.decode(errors="replace")
        hint = ""
        if "ensurepip" in err_msg or "venv" in err_msg.lower():
            hint = f" Hint: python3-venv may not be installed — run: sudo apt-get install -y {Path(py).name}-venv"
        raise HTTPException(500, f"venv creation failed: {err_msg}{hint}")


async def _install_requirements(row: App) -> None:
    """Install a Python app's dependencies — requirements.txt, uv.lock,
    poetry.lock, Pipfile.lock or pyproject.toml, whichever it has."""
    from venvy import appspec
    wd = appspec.work_dir(row)
    if not await asyncio.to_thread((Path(row.venv_path) / "bin" / "python").exists):
        raise HTTPException(500, "The virtual environment is missing its python.")
    steps = await asyncio.to_thread(runtimes.python_steps, wd, row.venv_path)
    await _run_steps(row, steps)


async def _create_environment(row: App, version: Optional[str] = None) -> None:
    """What an app needs to run, from the files it has: a virtualenv and its requirements (Python),
    or the dependencies its manifest lists and its build (the others)."""
    from venvy import appspec
    if row.runtime == "python":
        await _make_venv(row, version)
        await _install_requirements(row)
        return
    wd = appspec.work_dir(row)
    await _run_steps(row, await asyncio.to_thread(runtimes.setup_steps, row.runtime, wd, row.entrypoint, row))


@router.post("/apps/upload")
async def upload_app(
    name: str = Form(...),
    runtime: str = Form("python"),
    python_version: str = Form(""),
    entrypoint: str = Form(""),
    start_on_boot: bool = Form(False),
    restart_policy: str = Form("never"),
    files: Optional[List[UploadFile]] = File(None),
    requirements: Optional[UploadFile] = File(None),
    start_command: str = Form(""),
    root_dir: str = Form(""),
    runtime_version: str = Form(""),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Create an app. runtime "auto" picks one from the uploaded files (venvy.toml,
    Cargo.toml, go.mod, package.json, a Dockerfile …)."""
    from venvy import appspec, toolchains
    auto = runtime == "auto"
    if not auto and runtime not in runtimes.RUNTIMES:
        raise HTTPException(400, runtimes.RUNTIME_ERROR + " (or auto)")
    if restart_policy not in ("never", "on-failure", "always"):
        raise HTTPException(400, "restart_policy must be one of: never, on-failure, always")
    name = _clean_app_name(name)
    try:
        root_clean = appspec.clean_rel_path(root_dir, "root folder")
        start_clean = appspec.clean_command(start_command, "start command")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    def _check_runtime(rt: str, ep: str, ver: str):
        version = None
        if rt in runtimes.ROOT_RUNTIMES and user.role != "admin":
            raise HTTPException(403, runtimes.ROOT_RUNTIME_ERROR)
        try:
            ep_clean = runtimes.clean_entrypoint(rt, ep)
            if rt == "python":
                version = runtimes.clean_python_version(ver, allow_install=True)
            else:
                version = runtimes.clean_runtime_version(rt, ver) if ver and rt in toolchains.RUNTIME_KIND else None
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not (version and rt != "python") and not (start_clean and rt not in ("docker", "compose")):
            missing = runtimes.tools_error(rt)
            if missing:
                raise HTTPException(400, missing)
        return ep_clean, version
    if not auto:
        entrypoint_clean, python_version = _check_runtime(runtime, entrypoint, python_version or runtime_version)

    slug = sanitize_slug(name)
    user_apps_dir = APPS_BASE_DIR / str(user.id)
    await asyncio.to_thread(user_apps_dir.mkdir, exist_ok=True)

    app_dir = user_apps_dir / slug
    if await asyncio.to_thread(app_dir.exists):
        slug = f"{slug}-{int(time.time())}-{secrets.token_hex(3)}"
        app_dir = user_apps_dir / slug
    await asyncio.to_thread(app_dir.mkdir, parents=True, mode=0o750)
    if not _isolation_active():
        await asyncio.to_thread((app_dir / "logs").mkdir, mode=0o750)

    skipped_files: List[Dict[str, str]] = []
    try:
        for file in (files or []):
            rel_path = file.filename or "upload"
            reason = _upload_rel_path_error(rel_path)
            dest = None
            if not reason:
                try:
                    dest = (app_dir / rel_path).resolve()
                except (OSError, ValueError):
                    reason = "invalid path"
            if not reason and (dest is None or not is_safe_path(app_dir, dest)):
                reason = "path escapes app directory"
            if reason:
                skipped_files.append({"path": rel_path, "reason": reason})
                continue
            await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
            if not await asyncio.to_thread(_save_upload_file, file, dest, _SINGLE_REQUEST_UPLOAD_MAX):
                raise HTTPException(413, f"File '{rel_path}' exceeds the {_SINGLE_REQUEST_UPLOAD_MAX // (1024 * 1024)} MB single-request limit.")
        req_bytes = None
        if requirements and requirements.filename:
            req_bytes = await requirements.read(_REQUIREMENTS_MAX_BYTES + 1)
            if len(req_bytes) > _REQUIREMENTS_MAX_BYTES:
                raise HTTPException(413, "requirements.txt is larger than 1 MB.")
    except BaseException:
        # Nothing refers to this directory yet — the app's row is only
        # created below — so a failure here (a full disk, an oversized file,
        # the client hanging up) would leave it behind for good.
        await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
        raise

    if auto:
        detected, det_entry, why = await asyncio.to_thread(appspec.detect_runtime, app_dir / root_clean if root_clean else app_dir)
        if detected is None or detected not in runtimes.RUNTIMES:
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise HTTPException(400, "Could not tell what kind of app this is from its files — pick a runtime.")
        runtime = detected
        try:
            entrypoint_clean, python_version = _check_runtime(runtime, entrypoint or det_entry, python_version or runtime_version)
        except HTTPException:
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            raise
        logging.info("New app: runtime %s (%s)", runtime, why)

    venv_path = app_dir / "venv"
    new_app = App(
        user_id=user.id,
        name=name,
        slug=slug,
        runtime=runtime,
        app_dir=str(app_dir),
        venv_path=str(venv_path),
        entrypoint=entrypoint_clean,
        runtime_version=python_version,
        start_on_boot=start_on_boot,
        restart_policy=restart_policy,
        port=None,
        status="installing",
        start_command=start_clean,
        root_dir=root_clean,
    )
    session.add(new_app)
    session.commit()
    session.refresh(new_app)
    app_id = new_app.id
    audit_log(user.username, "app_create", f"created app '{new_app.name}' (id={app_id})")
    if user.role != "admin" and not user.all_apps:
        session.add(AppAccess(user_id=user.id, app_id=app_id))     # what you make, you can use
        session.commit()

    async def _finish() -> dict:
        """venv + pip install, which can take minutes — runs as a job."""
        try:
            with Session(engine) as s2:
                row = s2.get(App, app_id)
            if not row:
                raise HTTPException(404, "App no longer exists.")
            await asyncio.to_thread(_ensure_app_user, row)
            if runtime == "python":
                if python_version and python_version not in runtimes.python_versions():
                    from venvy import toolchains as _tc
                    await _tc.install("python", python_version)
                await _make_venv(row, python_version)

            if req_bytes is not None and runtime == "python":
                await asyncio.to_thread(_fs_save, app_dir, ("requirements.txt",), req_bytes, _app_ids(row))
            if runtime == "python":
                await _install_requirements(row)

            if runtime != "python":
                # Whatever the files uploaded with the app list, if any — and
                # the build (cargo build --release, go build …). Files that
                # arrive afterwards are installed and built on the first start.
                from venvy import appspec as _aspec
                wd = _aspec.work_dir(row)
                await _run_steps(row, await asyncio.to_thread(runtimes.setup_steps, runtime, wd, row.entrypoint, row))

            with Session(engine) as s2:
                row = s2.get(App, app_id)
                if not row:
                    raise HTTPException(404, "App no longer exists.")
                row.status = "stopped"
                s2.add(row)
                s2.commit()
                s2.refresh(row)
                result = _app_safe(row)
            result["skipped_files"] = skipped_files
            return result
        except BaseException:
            # Failed or cancelled: remove the half-created app entirely.
            # Stop/remove the user before deleting files so nothing recreates them.
            await asyncio.to_thread(_delete_app_user, app_id)
            await asyncio.to_thread(shutil.rmtree, app_dir, ignore_errors=True)
            with Session(engine) as s2:
                s2.exec(sql_delete(AppAccess).where(AppAccess.app_id == app_id))
                row = s2.get(App, app_id)
                if row:
                    s2.delete(row)
                s2.commit()
            raise

    return {**_start_job(user, "app_create", _finish, app_id=app_id, min_role="operator"),
            "app_id": app_id, "skipped_files": skipped_files}


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
    app_row_parent = app_row.parent_app_id

    from venvy import groups

    async def _do_start():
        try:
            with Session(engine) as s:
                await start_app_process_logic(app_id, s)
            if not app_row_parent:
                await groups.start_children(app_id)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background start failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                # A failed start has marked itself; an app that is running (one
                # of its processes failed) must not be taken out of the proxy.
                if a and not (a.pid and a.status == "running"):
                    a.status = "error"
                    s.add(a)
                    s.commit()
        finally:
            _running_tasks.discard(asyncio.current_task())

    async def _do_stop():
        try:
            if not app_row_parent:
                await groups.stop_children(app_id)
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
            if app_row_parent:
                await restart_app_process_logic(app_id)
            else:
                await groups.restart_group(app_id)
            _trigger_supervisor()
        except Exception as exc:
            logging.error(f"Background restart failed for app {app_id}: {exc}")
            with Session(engine) as s:
                a = s.get(App, app_id)
                if a and not (a.pid and a.status == "running"):
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
        rolling = not app_row_parent and (app_row.instances > 1 or app_row.zero_downtime)
        if not rolling:
            # (A rolling restart takes each process out of rotation itself, when its turn comes.)
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
    request: Request,
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
        if len(req.manual_ports) > 64:
            raise HTTPException(400, "At most 64 manual ports are allowed.")
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
        if len(req.env_vars) > 500:
            raise HTTPException(400, "At most 500 environment variables are allowed per app.")
        for env_key, env_value in req.env_vars.items():
            # Anything else can't be put in a process environment at all, and
            # only failed later, as an unexplained error on start.
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
                raise HTTPException(400, f"Invalid environment variable name: {env_key!r}")
            if is_dangerous_env_var(env_key):
                raise HTTPException(400, f"Environment variable {env_key!r} is not allowed for security reasons.")
            if "\x00" in env_value:
                raise HTTPException(400, f"Environment variable {env_key} contains a NUL character.")
        _record_env_change(session, app_row, req.env_vars, user.username)
        app_row.env_json = _encrypt_secret(json.dumps(req.env_vars))
        audit_log(user.username, "app_env_change", f"app '{app_row.name}' (id={app_id})")
    # Runtime first: the entrypoint rules below depend on it, and a request
    # that switches a Rust app to Python *and* names its entrypoint used to
    # have that entrypoint overwritten with main.rs.
    if req.runtime is not None:
        if req.runtime not in runtimes.RUNTIMES:
            raise HTTPException(400, runtimes.RUNTIME_ERROR)
        if req.runtime != app_row.runtime:
            if req.runtime in runtimes.ROOT_RUNTIMES and user.role != "admin":
                raise HTTPException(403, runtimes.ROOT_RUNTIME_ERROR)
            if runtimes.RUNTIMES[app_row.runtime if app_row.runtime in runtimes.RUNTIMES else "python"].entry == runtimes.FIXED:
                app_row.entrypoint = ""     # the old runtime's main.rs means nothing to the new one
            app_row.runtime = req.runtime
        if runtimes.RUNTIMES[app_row.runtime].entry == runtimes.FIXED:
            app_row.entrypoint = runtimes.RUNTIMES[app_row.runtime].entry_default
    if req.entrypoint is not None:
        try:
            app_row.entrypoint = runtimes.clean_entrypoint(app_row.runtime, req.entrypoint)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
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

    if req.healthcheck_path is not None:
        try:
            app_row.healthcheck_path = monitor.normalize_path(req.healthcheck_path)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        monitor.forget_health(app_id)
    if req.health_interval is not None:
        if not (5 <= req.health_interval <= 3600):
            raise HTTPException(400, "health_interval must be between 5 and 3600 seconds.")
        app_row.health_interval = req.health_interval
    if req.health_failures is not None:
        if not (1 <= req.health_failures <= 20):
            raise HTTPException(400, "health_failures must be between 1 and 20.")
        app_row.health_failures = req.health_failures
    if req.health_restart is not None:
        app_row.health_restart = req.health_restart
    if req.domain is not None:
        new_domain = _clean_domain(req.domain)
        if new_domain and (new_domain == own_hosts.host_name(request.headers.get("host", ""))
                           or own_hosts.is_own(new_domain)):
            raise HTTPException(400, "That is an address Venvy itself is reached under; an app cannot take it over.")
        if new_domain:
            other = session.exec(select(App).where(App.domain == new_domain, App.id != app_id)).first()
            if other:
                if has_app_access(session, user, other.id):
                    raise HTTPException(409, f"{new_domain} already belongs to '{other.name}'.")
                raise HTTPException(409, f"{new_domain} is already in use by another app.")
            # Another app's route on this hostname: taking the hostname as a domain
            # takes its requests too — unless it is a path route of an app the
            # same user manages (example.com -> this app, example.com/api -> that one).
            from venvy.models import CfRoute, Route
            for r in session.exec(select(Route).where(Route.hostname == new_domain, Route.app_id != app_id)).all():
                if not has_app_access(session, user, r.app_id) or r.path_prefix in ("", "/"):
                    raise HTTPException(409, f"{new_domain} is already routed to another app (Hosting → Routes).")
            if session.exec(select(CfRoute).where(CfRoute.hostname == new_domain, CfRoute.app_id != app_id)).first():
                raise HTTPException(409, f"{new_domain} is published for another app through the tunnel.")
        if new_domain != app_row.domain:
            audit_log(user.username, "app_domain_change", f"app '{app_row.name}' (id={app_id}): {new_domain or 'removed'}")
        app_row.domain = new_domain
    # An app served on its own domain proxies Host-matched traffic to its local
    # port, so apply the same port ownership check as Cloudflare publishing.
    if app_row.domain and app_row.port and (
        req.domain is not None or req.port is not None or req.manual_ports is not None
    ):
        await _check_publish_port(session, app_row, app_row.port, request)
    if req.disk_limit_mb is not None:
        if req.disk_limit_mb != 0 and not (1 <= req.disk_limit_mb <= 100_000_000):
            raise HTTPException(400, "disk_limit_mb must be a positive number of MB (or 0 for no limit).")
        app_row.disk_limit_mb = None if req.disk_limit_mb == 0 else req.disk_limit_mb

    from venvy import groups, secrets_ext
    spec_fields = {k: getattr(req, k) for k in SPEC_FIELDS if getattr(req, k) is not None}
    if app_row.parent_app_id and set(spec_fields) - {"start_command"}:
        raise HTTPException(400, "A worker or replica runs with the settings of its app — change them there.")
    try:
        changed = apply_spec_fields(app_row, spec_fields, admin=user.role == "admin")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if req.preview_env is not None:
        for k, v in req.preview_env.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or is_dangerous_env_var(k) or "\x00" in v:
                raise HTTPException(400, f"Invalid preview variable: {k!r}")
        app_row.preview_env = _encrypt_secret(json.dumps(req.preview_env)) if req.preview_env else ""
    if req.secret_files is not None:
        try:
            app_row.secret_files = secrets_ext.clean_files(req.secret_files)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        audit_log(user.username, "app_secret_files_change", f"app '{app_row.name}' (id={app_id}): {len(req.secret_files)} file(s)")
    if changed:
        audit_log(user.username, "app_settings_change", f"app '{app_row.name}' (id={app_id}): {', '.join(changed)}")

    app_row.updated_at = utc_now()
    session.add(app_row)
    session.commit()
    removed: List[int] = []
    if not app_row.parent_app_id and (req.instances is not None or req.healthcheck_path is not None
                                      or req.health_type is not None or req.restart_policy is not None):
        removed = groups.sync_replicas(session, app_row)
    if removed:
        await groups.delete_children(removed)
    proxy.invalidate()
    refreshed = session.get(App, app_id)
    return _app_safe(refreshed)


def _record_env_change(session: Session, app_row: App, new_vars: Dict[str, str], username: str) -> None:
    """Note which variables were added, removed or changed — names only, never values."""
    ok, plain = _try_decrypt_secret(app_row.env_json)
    try:
        old = json.loads(plain) if ok and plain else {}
    except ValueError:
        old = {}
    if not isinstance(old, dict):
        old = {}
    added = sorted(k for k in new_vars if k not in old)
    removed = sorted(k for k in old if k not in new_vars)
    changed = sorted(k for k in new_vars if k in old and str(old[k]) != new_vars[k])
    if not ok and new_vars:
        added = sorted(new_vars)                  # the old ones could not be read: all we can say
    if added or removed or changed:
        session.add(EnvHistory(app_id=app_row.id, username=username, added=",".join(added),
                               removed=",".join(removed), changed=",".join(changed)))


@router.get("/apps/{app_id}/env-history")
def env_history(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
):
    """Who changed which environment variables, newest first (names only)."""
    if not session.get(App, app_id):
        raise HTTPException(404)
    rows = session.exec(select(EnvHistory).where(EnvHistory.app_id == app_id).order_by(EnvHistory.id.desc()).limit(100)).all()
    split = lambda s: [x for x in s.split(",") if x]
    return [{"ts": r.ts.isoformat(), "user": r.username, "added": split(r.added),
             "removed": split(r.removed), "changed": split(r.changed)} for r in rows]


@router.get("/apps/{app_id}/metrics/history")
def app_metrics_history(
    app_id: int,
    minutes: int = Query(default=60, ge=1, le=10080),
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    if not session.get(App, app_id):
        raise HTTPException(404)
    return {"minutes": minutes, "samples": monitor.history(app_id, minutes)}


@router.get("/apps/{app_id}/disk")
async def app_disk(
    app_id: int,
    refresh: bool = False,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """What the app's folder takes on disk (measured at most every ten minutes unless refresh=true)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    size = await monitor.refresh_disk(app_row, force=refresh)
    return {"bytes": size, "limit_mb": app_row.disk_limit_mb}


@router.delete("/apps/{app_id}")
async def delete_app(
    app_id: int,
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    return await remove_app_completely(app_id, user.username)


async def remove_app_completely(app_id: int, username: str) -> dict:
    """Delete an app with everything that belongs to it (its processes, user,
    files, state, routes, published hostnames, history). Raises HTTPException."""
    with Session(engine) as session:
        return await _remove_app(session, app_id, username)


async def _remove_app(session: Session, app_id: int, username: str) -> dict:
    from venvy import appbackup, firewall, routing
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if app_row.status == "installing":
        raise HTTPException(409, "This app is still being set up — wait for its install to finish.")
    if deploy.is_running(app_id):
        raise HTTPException(409, "A deploy is running for this app — wait for it to finish.")
    app_name = app_row.name
    warnings: List[str] = []
    from venvy import appspec, groups, launcher
    from venvy.models import AppVolume, PortForward, Route, ServiceBinding
    if app_row.parent_app_id:
        # A worker or replica: its folder and user are its app's.
        async with _app_lock(app_id):
            await _stop_app_process(app_id, session)
            row = session.get(App, app_id, populate_existing=True)
            if row is not None:
                await asyncio.to_thread(shutil.rmtree, APP_LOGS_DIR / str(app_id), True)
                with contextlib.suppress(OSError):
                    _app_log_file(row).unlink()
                for model in (MetricSample,):
                    session.exec(sql_delete(model).where(model.app_id == app_id))
                session.delete(row)
                session.commit()
        forget_app_runtime(app_id)
        monitor.forget_app(app_id)
        launcher.forget(app_id)
        proxy.invalidate()
        audit_log(username, "app_delete", f"deleted process '{app_name}' (id={app_id})")
        return {"ok": True, "warnings": warnings}
    users_of = session.exec(select(ServiceBinding).where(ServiceBinding.service_app_id == app_id)).all()
    if users_of:
        names = [a.name for a in (session.get(App, b.app_id) for b in users_of) if a]
        raise HTTPException(409, "This service is used by " + ", ".join(names) + " — detach it from them first.")
    await groups.delete_children([c.id for c in groups.children(session, app_id)])
    # The app's lock is held from the stop until its row is gone. Released in
    # between (as it used to be, across the Cloudflare calls below), a start
    # that was already queued — a click, or the supervisor applying a restart
    # policy — ran in that gap and left a process in a directory that was
    # then deleted from under it, belonging to no app at all.
    async with _app_lock(app_id):
        app_row = session.get(App, app_id, populate_existing=True)
        if not app_row:
            raise HTTPException(404)
        _cancel_log_task(app_id)
        await _stop_app_process(app_id, session)
        # Shells opened on the app keep its directory and its Unix user busy.
        await terminal_manager.close_for_app(app_id)
        # Before anything outside Venvy changes: a failure here leaves the app
        # and its published routes as they were, so the delete can be retried.
        if not await asyncio.to_thread(_delete_app_user, app_id):
            raise HTTPException(500, "Could not terminate all processes or remove the app's system user. Try again.")

        # An app has one route per published port. Every one of them has to go:
        # a hostname left in the tunnel keeps forwarding to that port, where the
        # next app to bind it would be exposed to the internet unasked.
        cf_routes = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all()
        published = {r.hostname for r in cf_routes if r.cf_route_id and r.hostname}
        published_lower = {h.lower() for h in published}
        if published:
            try:
                creds      = _get_cf_creds(session)
                account_id = creds["cf_account_id"]
                tunnel_id  = creds["cf_tunnel_id"]
                api_token  = creds["cf_api_token"]
                await _cf_update_ingress(
                    creds, lambda named: [r for r in named if (r.get("hostname") or "").lower() not in published_lower])
                for hostname in sorted(published):
                    try:
                        zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
                        if zone_id:
                            await _cf_delete_dns_cname(zone_id, hostname, api_token, tunnel_id=tunnel_id)
                        logging.info(f"CF route cleaned up for deleted app {app_id} ({hostname})")
                    except Exception as exc:
                        logging.warning(f"CF DNS cleanup failed for app {app_id} ({hostname}): {exc}")
                        warnings.append(f"The DNS record for {hostname} could not be removed — delete it in Cloudflare.")
            except Exception as exc:
                logging.warning(f"CF cleanup failed for app {app_id} during delete: {exc}")
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                warnings.append(
                    "Cloudflare could not be updated, so " + ", ".join(sorted(published))
                    + f" still point(s) at this server — remove the route(s) in the Cloudflare dashboard. ({detail})")
        for cf_route in cf_routes:
            session.delete(cf_route)

        app_dir_path = Path(app_row.app_dir)
        if await asyncio.to_thread(app_dir_path.exists):
            await asyncio.to_thread(shutil.rmtree, app_row.app_dir, ignore_errors=True)
        await asyncio.to_thread(shutil.rmtree, APP_LOGS_DIR / str(app_id), True)
        (TERMINAL_RC_DIR / f"app-{app_id}.bashrc").unlink(missing_ok=True)
        forget_app_runtime(app_id)
        _package_locks.pop(app_id, None)
        _packages_cache.pop(app_id, None)
        for model in (ScheduledJob, Release, EnvHistory, MetricSample, AppAccess, Route, PortForward,
                      ServiceBinding, AppVolume):
            session.exec(sql_delete(model).where(model.app_id == app_id))
        await asyncio.to_thread(appspec.remove_state, app_id)
        launcher.forget(app_id)
        if app_row.runtime == "docker" and not app_row.entrypoint:
            with contextlib.suppress(Exception):
                proc = await asyncio.create_subprocess_exec(
                    "docker", "image", "rm", "-f", runtimes.docker_image(app_id),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(proc.wait(), 60)
        session.exec(sql_delete(AlertRule).where(AlertRule.app_id == app_id))
        await asyncio.to_thread(deploy.forget_keys, app_id)
        monitor.forget_app(app_id)
        session.delete(app_row)
        session.commit()
        proxy.invalidate()
    _app_locks.pop(app_id, None)
    routing.forget_app(app_id)
    appbackup.forget(app_id)
    from venvy import edge
    with contextlib.suppress(Exception):
        await edge.tailscale_unpublish(app_id)
    with contextlib.suppress(Exception):
        from venvy import kv
        ng = kv.get_json(edge.NGROK_APPS_KEY, {}) or {}
        if ng.pop(str(app_id), None) is not None:
            kv.put_json(edge.NGROK_APPS_KEY, ng)
    for preview in session.exec(select(App).where(App.preview_of == app_id)).all():
        with contextlib.suppress(Exception):
            await _remove_app(session, preview.id, username)
    with contextlib.suppress(Exception):
        await firewall.apply()
    audit_log(username, "app_delete", f"deleted app '{app_name}' (id={app_id})")
    return {"ok": True, "warnings": warnings}


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


def _read_manifest(app_row: App, name: str) -> Optional[str]:
    """Text of a manifest in the app's directory (None if absent or unreadable),
    opened the symlink-safe way, 1 MB at most."""
    root = tuple(p for p in (app_row.root_dir or "").split("/") if p)
    try:
        f, _ = _fs_open_file(Path(app_row.app_dir), root + (name,))
    except AppPathError:
        return None
    try:
        with f:
            return f.read(_REQUIREMENTS_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


@router.get("/runtimes")
async def list_runtimes(user: User = Depends(get_current_user)):
    """The runtimes this Venvy can host and whether this host has what they need."""
    return await asyncio.to_thread(runtimes.describe)


_PACKAGES_LIST_SEM = asyncio.Semaphore(4)
_PACKAGES_CACHE_TTL = 5.0
_packages_cache: Dict[int, tuple] = {}


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
        def _load_cargo():
            try:
                manifest, _ = _fs_open_file(Path(app_row.app_dir), tuple(p for p in (app_row.root_dir or "").split("/") if p) + ("Cargo.toml",))
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
        return await asyncio.to_thread(_load_cargo)

    if app_row.runtime in runtimes.RUNTIMES and app_row.runtime != "python":
        return await asyncio.to_thread(runtimes.list_packages, app_row.runtime, lambda name: _read_manifest(app_row, name))

    if app_row.runtime != "python":
        raise HTTPException(400, "This app's runtime is not known to this Venvy.")

    venv_python = Path(app_row.venv_path) / "bin" / "python"
    if not await asyncio.to_thread(venv_python.exists):
        raise HTTPException(500, "Virtual environment python not found.")

    cached = _packages_cache.get(app_id)
    if cached and time.monotonic() - cached[0] < _PACKAGES_CACHE_TTL:
        return cached[1]

    # The venv's python runs app-controlled code (.pth files, sitecustomize),
    # so it runs as the app's user, never as Venvy's. Bounded by semaphore to
    # prevent concurrent subprocess exhaustion.
    async with _PACKAGES_LIST_SEM:
        await asyncio.to_thread(_ensure_app_user, app_row)
        proc = await asyncio.create_subprocess_exec(
            *_as_app_user(app_row, [str(venv_python), "-m", "pip", "list", "--format=json"]),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_app_tool_env(app_row),
            cwd=_TOOL_CWD,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            return []
    try:
        pkgs = json.loads(stdout)
        _packages_cache[app_id] = (time.monotonic(), pkgs)
        return pkgs
    except Exception:
        return []


# pip and cargo both rewrite shared state (site-packages, Cargo.toml and its
# lock file). Two of them at once in the same app — "Install All" in one tab,
# a manual install in another — can leave a half-written environment, so
# package changes queue up per app.
_package_locks: Dict[int, asyncio.Lock] = {}


async def _run_app_tool(app_row: App, argv: List[str], timeout: int, what: str,
                        cwd: Optional[str] = None, env_extra: Optional[Dict[str, str]] = None,
                        as_venvy: bool = False) -> None:
    """Run a package-manager command for an app (as the app's user); raise
    HTTPException with its stderr on failure or timeout. *cwd* is for tools
    that work on the directory they are started in. *as_venvy*: as Venvy,
    with a clean environment (docker build)."""
    from venvy import toolchains
    from venvy.isolation import _clean_root_env
    async with _package_locks.setdefault(app_row.id, asyncio.Lock()):
        await asyncio.to_thread(_ensure_app_user, app_row)
        from venvy import appspec
        try:
            await toolchains.ensure_for_app(app_row, appspec.work_dir(app_row), lambda m: logging.info(m))
        except Exception as exc:
            raise HTTPException(500, f"Could not install the toolchain: {exc}")
        env = _app_tool_env(app_row)
        env.update(env_extra or {})
        if as_venvy:
            from venvy import compose_guard
            try:
                spawn = await asyncio.to_thread(compose_guard.pinned_argv, app_row, list(argv))
            except compose_guard.ComposeRefused as exc:
                raise HTTPException(400, str(exc))
            env = {**_clean_root_env(), "PATH": runtimes.tool_search_path(), "DOCKER_BUILDKIT": "1"}
        else:
            spawn = _as_app_user(app_row, argv)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spawn,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd or _TOOL_CWD,
                start_new_session=True,
            )
        except FileNotFoundError:
            raise HTTPException(500, runtimes.tools_error(app_row.runtime, packages=True)
                                or f"{argv[0]} is not installed on this host.")
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)     # the tool and whatever it started
            with contextlib.suppress(Exception):
                await proc.wait()
            raise HTTPException(500, f"{what} timed out after {timeout} seconds.")
        if proc.returncode != 0:
            # Some tools (go, composer, bundler) explain themselves on stdout.
            detail = (stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip())[-4000:]
            raise HTTPException(500, f"{what} failed: {detail}")


async def _run_steps(app_row: App, steps: List["runtimes.Step"]) -> None:
    from venvy import appspec
    wd = str(appspec.work_dir(app_row))
    for step in steps:
        await _run_app_tool(app_row, step.argv, step.timeout, step.what,
                            cwd=wd if step.in_app_dir else None, env_extra=dict(step.env), as_venvy=step.as_venvy)


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
        from venvy import appspec as _aspec
        argv = ["cargo", "add", pkg_name, "--manifest-path", str(_aspec.work_dir(app_row) / "Cargo.toml")]
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
    elif runtimes.can_install(app_row.runtime):
        try:
            from venvy import appspec as _aspec
            steps = runtimes.install_steps(app_row.runtime, _aspec.work_dir(app_row), pkg_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        argv = None
    else:
        raise HTTPException(400, "This app's runtime has no packages to install.")

    async def _job() -> dict:
        with Session(engine) as s:
            row = s.get(App, app_id)
        if not row:
            raise HTTPException(404, "App no longer exists.")
        if argv is None:
            await _run_steps(row, steps)
        else:
            await _run_app_tool(row, argv, timeout, what)
        _packages_cache.pop(app_id, None)
        audit_log(user.username, "package_install", f"app '{row.name}' (id={app_id}): {pkg_name}")
        return {"ok": True}

    return _start_job(user, "package_install", _job, app_id=app_id, min_role="operator")


@router.delete("/apps/{app_id}/packages/{name:path}")
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
    if not runtimes.can_install(app_row.runtime) and (
            not re.fullmatch(r"[A-Za-z0-9_.+\-]+", name) or name.startswith("-")):
        raise HTTPException(400, "Invalid package name.")

    if app_row.runtime == "rust":
        from venvy import appspec as _aspec
        argv = ["cargo", "remove", name, "--manifest-path", str(_aspec.work_dir(app_row) / "Cargo.toml")]
        timeout, what = 120, "cargo remove"
    elif app_row.runtime == "python":
        venv_python = Path(app_row.venv_path) / "bin" / "python"
        if not venv_python.exists():
            raise HTTPException(500, "Virtual environment python not found.")
        argv = [str(venv_python), "-m", "pip", "uninstall", "-y", name]
        timeout, what = 300, "pip uninstall"
    elif runtimes.can_install(app_row.runtime):
        try:
            from venvy import appspec as _aspec
            steps = runtimes.remove_steps(app_row.runtime, _aspec.work_dir(app_row), name)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        argv = None
    else:
        raise HTTPException(400, "This app's runtime has no packages to remove.")

    async def _job() -> dict:
        with Session(engine) as s:
            row = s.get(App, app_id)
        if not row:
            raise HTTPException(404, "App no longer exists.")
        if argv is None:
            await _run_steps(row, steps)
        else:
            await _run_app_tool(row, argv, timeout, what)
        _packages_cache.pop(app_id, None)
        audit_log(user.username, "package_uninstall", f"app '{row.name}' (id={app_id}): {name}")
        return {"ok": True}

    return _start_job(user, "package_uninstall", _job, app_id=app_id, min_role="operator")


class VenvReq(BaseModel):
    version: str = ""        # "" = the system's python3


@router.post("/apps/{app_id}/venv/rebuild")
async def rebuild_venv(
    app_id: int,
    req: VenvReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Throw the virtualenv away and build it again — with another Python version if asked —
    then reinstall requirements.txt. The app must be stopped. Answers {"job_id"}."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if app_row.runtime != "python":
        raise HTTPException(400, "Only Python apps have a virtual environment.")
    if restore_in_progress():
        raise HTTPException(409, "A restore is in progress.")
    if deploy.is_running(app_id):
        raise HTTPException(409, "A deploy is running for this app — wait for it to finish.")
    if app_row.status in ("running", "starting", "stopping", "installing"):
        raise HTTPException(409, "Stop the app first.")
    try:
        version = runtimes.clean_python_version(req.version)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    async def _job() -> dict:
        async with _app_lock(app_id), _package_locks.setdefault(app_id, asyncio.Lock()):
            if restore_in_progress():
                raise HTTPException(409, "A restore is in progress.")
            if deploy.is_running(app_id):
                raise HTTPException(409, "A deploy is running for this app — wait for it to finish.")
            with Session(engine) as s:
                row = s.get(App, app_id)
                if not row:
                    raise HTTPException(404, "App no longer exists.")
                if row.status in ("running", "starting", "stopping", "installing"):
                    raise HTTPException(409, "Stop the app first.")
                app_name = row.name
                row.status = "installing"
                s.add(row)
                s.commit()
                s.refresh(row)
            try:
                await asyncio.to_thread(_ensure_app_user, row)
                proc = await asyncio.create_subprocess_exec(
                    *_as_app_user(row, ["rm", "-rf", str(row.venv_path)]), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, cwd=_TOOL_CWD)
                await proc.wait()
                await _make_venv(row, version)
                await _install_requirements(row)
                with Session(engine) as s:
                    row = s.get(App, app_id)
                    if row:
                        row.runtime_version = version
                        row.status = "stopped"
                        s.add(row)
                        s.commit()
            except BaseException:
                with Session(engine) as s:
                    row = s.get(App, app_id)
                    if row and row.status == "installing":
                        row.status = "stopped"
                        s.add(row)
                        s.commit()
                raise
        _packages_cache.pop(app_id, None)
        audit_log(user.username, "app_venv_rebuild", f"app '{app_name}' (id={app_id}) python={version or 'default'}")
        return {"ok": True, "version": version}

    return _start_job(user, "venv_rebuild", _job, app_id=app_id, min_role="operator")


@router.post("/apps/{app_id}/packages/sync")
async def sync_packages(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Install everything the app's manifest (package.json, Gemfile, …) lists.
    Answers {"job_id"} — poll GET /api/jobs/{id}."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    from venvy import appspec
    wd = appspec.work_dir(app_row)
    if app_row.runtime == "python":
        steps = runtimes.python_steps(wd, app_row.venv_path)
    else:
        steps = runtimes.sync_steps(app_row.runtime, wd) + runtimes.build_steps(app_row.runtime, wd, app_row.entrypoint, app_row)
    if not steps:
        raise HTTPException(400, "There is no manifest in the app's folder to install from.")

    async def _job() -> dict:
        with Session(engine) as s:
            row = s.get(App, app_id)
        if not row:
            raise HTTPException(404, "App no longer exists.")
        await _run_steps(row, steps)
        _packages_cache.pop(app_id, None)
        audit_log(user.username, "package_sync", f"app '{row.name}' (id={app_id})")
        return {"ok": True}

    return _start_job(user, "package_sync", _job, app_id=app_id, min_role="operator")


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

    def _clear() -> None:
        if log_file.exists() and not log_file.is_symlink():
            fd = os.open(log_file, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            os.close(fd)

    await asyncio.to_thread(_clear)
    audit_log(user.username, "app_logs_clear", f"app '{app_row.name}' (id={app_id})")
    return {"ok": True}


# Directories the import scanner does not descend into: none of them holds
# the app's own code, and a virtualenv alone is tens of thousands of files.
_SCAN_SKIP_DIRS = {"venv", ".venv", "__pycache__", "node_modules", "site-packages", "logs", "target",
                   ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache", ".tox"}


_SCAN_MAX_FILES = 5000


_SCAN_MAX_FILE_BYTES = 2 * 1024 * 1024


def _scan_imported_modules(root: Path) -> Set[str]:
    """Top-level module names imported by the Python files under *root*.
    Blocking — a large tree takes a while to read and parse."""
    imported: Set[str] = set()
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            seen += 1
            if seen > _SCAN_MAX_FILES:
                return imported
            try:
                parts = Path(dirpath, name).relative_to(root).parts
                fileobj, st = _fs_open_file(root, parts)
                with fileobj:
                    if st.st_size > _SCAN_MAX_FILE_BYTES:
                        continue
                    raw = fileobj.read(_SCAN_MAX_FILE_BYTES + 1)
                    if len(raw) > _SCAN_MAX_FILE_BYTES:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                tree = ast.parse(text)
            except (AppPathError, OSError, SyntaxError, ValueError, RecursionError, MemoryError):
                # Not a plain file we can reach without following a link, or not
                # parseable Python (binary content raises ValueError, not SyntaxError).
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.level == 0:
                        imported.add(node.module.split(".")[0])
    return imported


@router.get("/apps/{app_id}/scan-imports")
async def scan_imports(
    app_id: int,
    session: Session = Depends(get_session),
    # Reads every .py file in the app: file contents, which a viewer doesn't get.
    user: User = Depends(require_operator),
):
    """Scan imported packages to discover dependencies missing from the active venv."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    root = Path(app_row.app_dir)
    venv_python = Path(app_row.venv_path) / "bin" / "python"

    installed: Set[str] = set()
    if venv_python.exists():
        await asyncio.to_thread(_ensure_app_user, app_row)
        proc = await asyncio.create_subprocess_exec(
            *_as_app_user(app_row, [str(venv_python), "-m", "pip", "list", "--format=json"]),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_app_tool_env(app_row),
            cwd=_TOOL_CWD,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
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

    # Off the event loop: walking and parsing a big tree here froze every
    # other request, terminal and log stream until it was done.
    imported = await asyncio.to_thread(_scan_imported_modules, root)

    # A module that is a file or package in the app itself is not a missing
    # dependency.
    local = set()
    with contextlib.suppress(OSError):
        for entry in os.scandir(root):
            local.add(entry.name[:-3] if entry.name.endswith(".py") else entry.name)

    missing = sorted(
        name for name in imported
        if name
        and name not in stdlib
        and name.lower() not in installed
        and name not in local
        and not name.startswith("_")
    )

    return {"missing": missing, "installed_count": len(installed)}
