"""Backing up and restoring one app.

An app backup is a tar.gz with:

    app.json      the app's settings (variables stay encrypted with this
                  server's key; they come back only on a server with it)
    files/        the app's folder
    data/         its $APP_DATA_DIR

made by the app's own user (so nothing it doesn't own can end up in it), with
every SQLite database copied through SQLite's backup API — a consistent copy
even while the app writes to it. Running services dump themselves first.

Restoring stops the app, unpacks the archive (again as the app's user) next
to it, swaps the folders in, and starts it again if it was running. The
replaced files are kept until the swap is done, so a failed restore leaves
the app as it was.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import os
import re
import shutil
import tarfile
from pathlib import Path
from typing import List, Optional

from sqlmodel import Session

from venvy import appspec
from venvy.config import BACKUP_DIR, utc_now
from venvy.db import engine
from venvy.isolation import _as_app_user, _app_tool_env, _ensure_app_user, _resolve_system_bin, _isolation_active
from venvy.models import App

APP_BACKUP_DIR = BACKUP_DIR / "apps"
_ARCHIVE = Path(__file__).parent / "runtime_assets" / "app_archive.py"
NAME_RE = re.compile(r"app-(\d+)-\d{8}-\d{6}-[0-9a-f]{6}\.tar\.gz")
_SKIP_FIELDS = {"pid", "pid_start", "status", "systemd_scope", "detected_ports", "last_exit_code", "last_start_time",
                "last_stop_time", "last_manually_stopped_at", "app_dir", "venv_path", "id", "user_id"}


def _dir(app_id: int) -> Path:
    return APP_BACKUP_DIR / str(app_id)


def list_backups(app_id: int) -> List[dict]:
    d = _dir(app_id)
    out = []
    if d.is_dir():
        for f in sorted(d.glob("app-*.tar.gz"), reverse=True):
            with contextlib.suppress(OSError):
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size,
                            "created_at": datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc).isoformat()})
    return out


def path_of(app_id: int, name: str) -> Path:
    m = NAME_RE.fullmatch(name or "")
    if not m or int(m.group(1)) != app_id:
        raise ValueError("No such backup.")
    p = _dir(app_id) / name
    if not p.is_file():
        raise ValueError("No such backup.")
    return p


def _python() -> str:
    return _resolve_system_bin("python3") if _isolation_active() else "python3"


async def create(app_id: int) -> dict:
    from venvy import services
    with Session(engine) as s:
        app = s.get(App, app_id)
    if app is None:
        raise ValueError("App not found.")
    if app.service_template:
        await services.dump_all()
    await asyncio.to_thread(_ensure_app_user, app)
    d = _dir(app_id)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    APP_BACKUP_DIR.chmod(0o700)
    name = f"app-{app_id}-{utc_now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.tar.gz"
    inner = d / f".{name}.inner"
    argv = _as_app_user(app, [_python(), "-I", str(_ARCHIVE), "archive",
                              f"{app.app_dir}=files", f"{appspec.data_dir(app)}=data"])
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env=_app_tool_env(app), cwd="/",
                                                start_new_session=True)
    assert proc.stdout is not None
    size = 0
    with open(inner, "wb") as f:
        while True:
            chunk = await proc.stdout.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > 20 * 1024 ** 3:
                proc.kill()
                break
            f.write(chunk)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        inner.unlink(missing_ok=True)
        raise RuntimeError("The app's files could not be archived: " + err.decode(errors="replace")[-300:])
    meta = {k: v for k, v in app.model_dump().items() if k not in _SKIP_FIELDS}
    for k, v in list(meta.items()):
        if isinstance(v, datetime.datetime):
            meta[k] = v.isoformat()
    meta["_venvy_app_backup"] = 1

    def wrap() -> int:
        out = d / name
        with tarfile.open(out, "w:gz") as tar:
            data = json.dumps(meta, indent=1).encode()
            ti = tarfile.TarInfo("app.json")
            ti.size = len(data)
            ti.mtime = int(utc_now().timestamp())
            import io
            tar.addfile(ti, io.BytesIO(data))
            tar.add(inner, arcname="content.tar.gz")
        out.chmod(0o600)
        return out.stat().st_size
    try:
        total = await asyncio.to_thread(wrap)
    finally:
        inner.unlink(missing_ok=True)
    return {"name": name, "size": total}


async def restore(app_id: int, name: str, settings: bool = False, log=lambda m: logging.info(m)) -> dict:
    """Put the files (and with *settings*, the settings) of a backup back."""
    from venvy import process
    from venvy.deploy import _swap
    archive = path_of(app_id, name)
    with Session(engine) as s:
        app = s.get(App, app_id)
    if app is None:
        raise ValueError("App not found.")
    ids = await asyncio.to_thread(_ensure_app_user, app)
    work = appspec.state_root(app) / "restore"

    def prep():
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(mode=0o700)
        if ids:
            os.chown(work, ids[0], ids[1])
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            if set(names) - {"app.json", "content.tar.gz"}:
                raise ValueError("That is not an app backup.")
            meta = json.loads(tar.extractfile("app.json").read())
            inner = work / "content.tar.gz"
            with tar.extractfile("content.tar.gz") as src, open(inner, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if ids:
                os.chown(inner, ids[0], ids[1])
        return meta
    try:
        meta = await asyncio.to_thread(prep)
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, work, True)
        raise
    was_running = app.status == "running"
    with Session(engine) as s:
        await process.stop_app_process_logic(app_id, s, manual=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *_as_app_user(app, ["tar", "-xzf", str(work / "content.tar.gz"), "-C", str(work), "--no-same-owner"]),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            env=_app_tool_env(app), cwd="/", start_new_session=True)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("The backup could not be unpacked: " + err.decode(errors="replace")[-300:])
        files, data = work / "files", work / "data"
        if files.is_dir():
            await asyncio.to_thread(_swap, Path(app.app_dir), files)
        if data.is_dir():
            await asyncio.to_thread(_swap, appspec.data_dir(app), data)
    except BaseException:
        # As it was: the app's own files were not replaced (a swap is all or
        # nothing), so it starts again on them.
        await asyncio.to_thread(shutil.rmtree, work, True)
        if was_running:
            with contextlib.suppress(Exception):
                with Session(engine) as s:
                    await process.start_app_process_logic(app_id, s)
        raise
    await asyncio.to_thread(shutil.rmtree, work, True)
    if settings:
        with Session(engine) as s:
            row = s.get(App, app_id)
            for k, v in meta.items():
                if (k.startswith("_") or k.endswith("_at") or k in _SKIP_FIELDS
                        or k in ("name", "slug", "domain", "port", "manual_ports", "parent_app_id", "preview_of")):
                    continue
                if hasattr(row, k) and not isinstance(getattr(App, k, None), property):
                    with contextlib.suppress(Exception):
                        setattr(row, k, v)
            s.add(row)
            s.commit()
    if was_running:
        with Session(engine) as s:
            await process.start_app_process_logic(app_id, s)
    return {"ok": True, "restarted": was_running}


def read_settings(app_id: int, name: str) -> dict:
    """The settings (app.json) stored in a backup. Raises ValueError."""
    p = path_of(app_id, name)
    try:
        with tarfile.open(p, "r:gz") as tar:
            f = tar.extractfile("app.json")
            meta = json.loads(f.read(1 << 20)) if f else None
    except (tarfile.TarError, KeyError, OSError, ValueError) as exc:
        raise ValueError(f"That is not an app backup: {exc}")
    if not isinstance(meta, dict):
        raise ValueError("That is not an app backup.")
    return meta


def delete(app_id: int, name: str) -> None:
    path_of(app_id, name).unlink()


def forget(app_id: int) -> None:
    shutil.rmtree(_dir(app_id), ignore_errors=True)
