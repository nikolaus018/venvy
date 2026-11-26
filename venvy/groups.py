"""An app and the processes that belong to it.

An app (process_kind "app") can have:

* workers — other commands of the same code (a Procfile's "worker:",
  venvy.toml's [processes]), with no port of their own;
* replicas — more copies of the app itself, each on its own port, behind
  Venvy's proxy (``instances``);
* a tunnel companion (ngrok) that publishes it.

Each is an App row with ``parent_app_id`` set, so the supervisor, restart
policies, logs and metrics treat them like any app. What they run with —
runtime, folder, environment, limits, sandbox — is always the parent's,
taken at start (``effective``); a worker only brings its command.
"""
import asyncio
import contextlib
import logging
import types
from typing import Dict, List, Optional

from sqlmodel import Session, select

from venvy.config import one_line
from venvy.db import engine
from venvy.models import App

# What a child never takes from its parent.
_OWN_FIELDS = {
    "id", "name", "slug", "status", "pid", "pid_start", "port", "manual_ports", "detected_ports",
    "external_url", "domain", "systemd_scope", "last_exit_code", "last_start_time", "last_stop_time",
    "last_manually_stopped_at", "created_at", "updated_at", "parent_app_id", "process_kind", "process_name",
    "instances", "zero_downtime", "previews_enabled", "preview_of", "preview_domain", "git_url", "webhook_secret",
    "auto_deploy", "last_deploy_at", "last_deploy_status", "last_deploy_commit", "start_on_boot", "restart_policy",
}
MAX_INSTANCES = 32


def is_child(app) -> bool:
    return bool(getattr(app, "parent_app_id", None))


def children(session: Session, app_id: int, kind: Optional[str] = None) -> List[App]:
    q = select(App).where(App.parent_app_id == app_id)
    if kind:
        q = q.where(App.process_kind == kind)
    return list(session.exec(q.order_by(App.id)).all())


def effective(row: App, session: Optional[Session] = None):
    """What *row* runs with: itself, or for a child its parent's settings with
    its own identity (and, for a worker, its own command)."""
    if not row.parent_app_id:
        return row
    if session is None:
        with Session(engine, expire_on_commit=False) as own:
            parent = own.get(App, row.parent_app_id)
    else:
        with session.no_autoflush:
            parent = session.get(App, row.parent_app_id)
    if parent is None:
        return row
    d = parent.model_dump()
    for k in _OWN_FIELDS:
        d[k] = getattr(row, k)
    if row.process_kind == "worker":
        d["start_command"] = row.start_command
        d["healthcheck_path"] = None
        d["health_type"] = "none"
        d["container_port"] = None
    elif row.process_kind == "tunnel":
        d["start_command"] = row.start_command
        d["runtime"] = "command"
        d["healthcheck_path"] = None
        d["health_type"] = "none"
        d["sandbox"] = True
        d["egress_policy"] = "allow"
    return types.SimpleNamespace(**d)


def _child(parent: App, kind: str, name: str, command: Optional[str]) -> App:
    return App(
        user_id=parent.user_id, name=f"{parent.name} · {name}"[:50], slug=parent.slug, runtime=parent.runtime,
        app_dir=parent.app_dir, venv_path=parent.venv_path, entrypoint=parent.entrypoint,
        parent_app_id=parent.id, process_kind=kind, process_name=name, start_command=command,
        restart_policy=parent.restart_policy if parent.restart_policy != "never" else "on-failure",
        start_on_boot=parent.start_on_boot, status="stopped",
        healthcheck_path=parent.healthcheck_path if kind == "replica" else None,
        health_type=parent.health_type if kind == "replica" else "none",
        health_interval=parent.health_interval, health_failures=parent.health_failures,
        health_restart=parent.health_restart, health_start_period=parent.health_start_period,
        health_command=parent.health_command if kind == "replica" else None,
        access_log=parent.access_log,
    )


def sync_replicas(session: Session, parent: App) -> List[int]:
    """Make the replica rows match parent.instances. Returns ids of removed
    replicas (the caller stops them)."""
    want = max(0, min(MAX_INSTANCES, int(parent.instances or 1)) - 1)
    have = children(session, parent.id, "replica")
    removed = []
    for extra in have[want:]:
        removed.append(extra.id)
    for i in range(len(have), want):
        session.add(_child(parent, "replica", f"web.{i + 2}", None))
    for r in have[:want]:
        # Health settings are read from the row by the monitor: keep them the parent's.
        r.healthcheck_path, r.health_type = parent.healthcheck_path, parent.health_type
        r.health_command, r.health_interval = parent.health_command, parent.health_interval
        r.health_failures, r.health_restart = parent.health_failures, parent.health_restart
        r.health_start_period = parent.health_start_period
        r.restart_policy = parent.restart_policy if parent.restart_policy != "never" else "on-failure"
        r.start_on_boot = parent.start_on_boot
        r.access_log = parent.access_log
        session.add(r)
    session.commit()
    return removed


def sync_workers(session: Session, parent: App, procs: Dict[str, str]) -> Dict[str, List]:
    """Make the worker rows match *procs* ({name: command}; "web" is the app
    itself and is skipped). Returns {"added": [...], "changed": [...], "removed": [ids]}."""
    have = {w.process_name: w for w in children(session, parent.id, "worker")}
    out: Dict[str, List] = {"added": [], "changed": [], "removed": []}
    for name, cmd in procs.items():
        if name == "web":
            continue
        w = have.pop(name, None)
        if w is None:
            session.add(_child(parent, "worker", name, cmd))
            out["added"].append(name)
        elif w.start_command != cmd:
            w.start_command = cmd
            session.add(w)
            out["changed"].append(w.id)
    for w in have.values():
        out["removed"].append(w.id)
    session.commit()
    return out


async def delete_children(ids: List[int]) -> None:
    """Stop and remove child rows (their user and folder are the parent's)."""
    from venvy import process
    for cid in ids:
        with contextlib.suppress(Exception):
            with Session(engine) as s:
                await process.stop_app_process_logic(cid, s, manual=True)
        with Session(engine) as s:
            row = s.get(App, cid)
            if row is not None and row.parent_app_id:
                s.delete(row)
                s.commit()
        process.forget_app_runtime(cid)
        # SQLite hands a deleted row's id to the next one: nothing of this
        # process (its health, its place in rotation) may stick to that one.
        from venvy import monitor, routing
        monitor.forget_app(cid)
        routing.undrain(cid)
        routing.inflight.pop(cid, None)


# ---------------------------------------------------------------------------
# Starting and stopping the whole group
# ---------------------------------------------------------------------------

def group_ids(app_id: int) -> List[int]:
    with Session(engine) as s:
        return [c.id for c in children(s, app_id) if c.process_kind in ("replica", "worker", "tunnel")]


async def start_children(app_id: int) -> None:
    from venvy import process
    for cid in group_ids(app_id):
        try:
            with Session(engine) as s:
                await process.start_app_process_logic(cid, s)
        except Exception as exc:
            logging.warning("Could not start process %s: %s", cid, one_line(exc))


async def stop_children(app_id: int) -> None:
    from venvy import process

    async def one(cid: int) -> None:
        try:
            with Session(engine) as s:
                await process.stop_app_process_logic(cid, s, manual=True)
        except Exception as exc:
            logging.warning("Could not stop process %s: %s", cid, one_line(exc))
    await asyncio.gather(*(one(c) for c in group_ids(app_id)))


async def _wait_ready(app_id: int, timeout: float) -> bool:
    """Until the process is running and (if it has one) its health check passes."""
    from venvy import monitor
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    await asyncio.sleep(1)
    while loop.time() < end:
        with Session(engine) as s:
            row = s.get(App, app_id)
        if row is None or row.status in ("stopped", "error"):
            return False
        if row.status == "running":
            ok = await monitor.check_once(row)
            if ok is None and row.port:
                # No health check: ready once it accepts connections on its port.
                ok = (await monitor.probe_tcp(row.port))["ok"]
            if ok is not False:
                return True
        await asyncio.sleep(1)
    return False


_group_locks: Dict[int, asyncio.Lock] = {}


def _still_replica(cid: int, app_id: int) -> bool:
    with Session(engine) as s:
        row = s.get(App, cid)
        return row is not None and row.parent_app_id == app_id and row.process_kind == "replica"


async def restart_group(app_id: int) -> None:
    """One rolling restart of an app at a time: two at once would take every copy
    out of rotation together."""
    lock = _group_locks.setdefault(app_id, asyncio.Lock())
    async with lock:
        await _restart_group(app_id)


async def _restart_group(app_id: int) -> None:
    """Restart an app and everything that belongs to it.

    With replicas, one at a time, each waiting until the one before is
    ready, so the proxy always has someone to send requests to. With
    zero_downtime and no replicas, a temporary one is started first and
    removed once the app itself is back."""
    from venvy import monitor, process, proxy
    with Session(engine) as s:
        row = s.get(App, app_id)
        if row is None:
            return
        replicas = [c.id for c in children(s, app_id, "replica")]
        others = [c.id for c in children(s, app_id) if c.process_kind in ("worker", "tunnel")]
        zero = bool(row.zero_downtime)
        rid = row.id
        wait = max(30, int(row.health_start_period or 0) + 30)
    surge = None
    if zero and not replicas:
        with Session(engine) as s:
            parent = s.get(App, app_id)
            surge_row = _child(parent, "replica", "surge", None)
            surge_row.restart_policy = "never"
            s.add(surge_row)
            s.commit()
            surge = surge_row.id
        with Session(engine) as s:
            await process.start_app_process_logic(surge, s)
        proxy.invalidate()
        if not await _wait_ready(surge, wait):
            logging.warning("Zero-downtime restart of app %s: the new copy did not become ready; restarting in place.", rid)
    from venvy import routing
    rolling = bool(replicas or surge)
    try:
        for target in [app_id] + replicas:
            if target != app_id and not _still_replica(target, app_id):
                continue        # removed (instances lowered) while this restart ran
            if rolling:
                # Out of rotation first, and let it finish what it is serving.
                routing.drain(target)
                await asyncio.sleep(0.3)        # a request that picked it a moment ago gets going
                await routing.wait_idle(target, 30)
            try:
                await process.restart_app_process_logic(target)
                proxy.invalidate()
                if rolling:
                    await _wait_ready(target, wait)
            except Exception as exc:
                if target == app_id:
                    raise
                # One replica that can't start is that replica's problem: the
                # others still get their restart, and the app is not in error.
                logging.warning("Rolling restart: process %s did not restart: %s", target, one_line(exc))
            finally:
                routing.undrain(target)
                proxy.invalidate()
        for cid in others:
            with contextlib.suppress(Exception):
                await process.restart_app_process_logic(cid)
    finally:
        if surge is not None:
            routing.drain(surge)
            await asyncio.sleep(0.3)
            await routing.wait_idle(surge, 30)
            await delete_children([surge])
            routing.undrain(surge)
            monitor.forget_app(surge)
            proxy.invalidate()
