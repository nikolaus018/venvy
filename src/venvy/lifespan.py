"""Application startup and shutdown.

Startup reconciles the database against reality — the previous process may have
been killed with apps still running — then starts the background tasks.
Shutdown stops every app Venvy launched, because leaving orphaned processes
holding ports behind is the one failure mode that makes the next start fail.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI
from sqlmodel import col, select

from venvy.core.process_compat import (
    HAS_PROCESS_GROUPS,
    get_pgid,
    own_pgid,
    pid_alive,
    signal_group,
    signal_pid,
)
from venvy.core.timeutils import utc_now
from venvy.db.engine import dispose_engine, session_scope
from venvy.db.migrations import run_migrations
from venvy.db.models import App, require_id
from venvy.services import registry
from venvy.services.metrics import metrics_collector_task
from venvy.services.processes import AppStartError, start_app
from venvy.services.provisioning import run_startup_checks
from venvy.services.supervisor import supervisor_task

__all__ = ["lifespan"]

logger = logging.getLogger(__name__)

#: How long every app gets to exit on SIGTERM during shutdown before we stop
#: waiting. Kept under the 10s systemd and Docker allow by default so the
#: escalation to SIGKILL still happens inside our own shutdown.
_SHUTDOWN_GRACE_SECONDS = 5.0
_SHUTDOWN_POLL_INTERVAL = 0.25


def _reconcile_stale_state() -> None:
    """Bring app rows in line with reality after a restart.

    Two things are fixed. Rows still marked ``running`` whose process is gone
    are cleared — the previous process died without recording the exit. And
    every app that is *not* start-on-boot is marked as manually stopped, so the
    restart policy does not resurrect apps the operator had deliberately left
    down before Venvy restarted.
    """
    boot_time = utc_now()
    with session_scope() as session:
        for app in session.exec(select(App)).all():
            modified = False

            if not app.start_on_boot:
                app.last_manually_stopped_at = boot_time
                modified = True

            if app.status == "running" and app.pid and not pid_alive(app.pid):
                logger.info("Clearing stale 'running' status for '%s'", app.name)
                app.status = "stopped"
                app.pid = None
                app.last_manually_stopped_at = boot_time
                modified = True

            if modified:
                session.add(app)
        session.commit()


async def _start_boot_apps() -> None:
    """Start every app flagged start-on-boot."""
    with session_scope() as session:
        boot_apps = list(session.exec(select(App).where(col(App.start_on_boot))).all())

    for app in boot_apps:
        if app.status == "running":
            continue
        logger.info("Boot-starting '%s'…", app.name)
        app_id = require_id(app.id)
        try:
            with session_scope() as session:
                await start_app(app_id, session)
        except (AppStartError, ValueError, OSError) as exc:
            logger.error("Boot-start failed for '%s': %s", app.name, exc)


def _collect_shutdown_targets() -> tuple[set[int], set[int]]:
    """Return the process groups and PIDs Venvy must terminate.

    Candidates are drawn from both the database and the in-memory registry: the
    database can be stale, and the registry only knows about this session.
    Venvy's own process group is excluded — signalling it would kill the
    shutdown sequence itself.

    On a platform without process groups the group set comes back empty and the
    PID set carries everything, so the caller degrades to per-process signals.
    """
    candidates: set[int] = set(registry.active_pids.values())
    try:
        with session_scope() as session:
            for app in session.exec(select(App).where(App.status == "running")).all():
                if app.pid:
                    candidates.add(app.pid)
    except Exception:
        logger.exception("Could not read running apps during shutdown")

    live = {pid for pid in candidates if pid_alive(pid)}
    if not HAS_PROCESS_GROUPS:
        return set(), live

    mine = own_pgid()
    groups: set[int] = set()
    ungrouped: set[int] = set()
    for pid in live:
        pgid = get_pgid(pid)
        if pgid is None:
            ungrouped.add(pid)
        elif pgid != mine:
            groups.add(pgid)
    return groups, ungrouped


async def _terminate_targets(groups: set[int], pids: set[int]) -> None:
    """SIGTERM everything, wait for it to exit, then SIGKILL the stragglers."""
    if not groups and not pids:
        return

    for pgid in groups:
        signal_group(pgid, signal.SIGTERM)
    for pid in pids:
        signal_pid(pid, signal.SIGTERM)

    deadline = time.monotonic() + _SHUTDOWN_GRACE_SECONDS
    remaining_groups, remaining_pids = set(groups), set(pids)
    while (remaining_groups or remaining_pids) and time.monotonic() < deadline:
        # Signal 0 delivers nothing; it only asks whether the target still exists.
        remaining_groups = {g for g in remaining_groups if signal_group(g, 0)}
        remaining_pids = {p for p in remaining_pids if pid_alive(p)}
        if not remaining_groups and not remaining_pids:
            break
        await asyncio.sleep(_SHUTDOWN_POLL_INTERVAL)

    for pgid in remaining_groups:
        logger.warning("Process group %s ignored SIGTERM; sending SIGKILL.", pgid)
        signal_group(pgid, signal.SIGKILL)
    for pid in remaining_pids:
        logger.warning("Process %s ignored SIGTERM; sending SIGKILL.", pid)
        signal_pid(pid, signal.SIGKILL)


def _mark_all_stopped() -> None:
    """Record every app as stopped, so the next start sees a clean slate."""
    try:
        with session_scope() as session:
            for app in session.exec(select(App).where(App.status == "running")).all():
                app.status = "stopped"
                app.pid = None
                if app.last_stop_time is None:
                    app.last_stop_time = utc_now()
                session.add(app)
            session.commit()
    except Exception:
        logger.exception("Could not record final app states during shutdown")


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Manage the application's startup and shutdown sequence."""
    logger.info("Venvy starting up…")
    run_migrations()
    await run_startup_checks()
    await asyncio.to_thread(_reconcile_stale_state)
    await _start_boot_apps()

    supervisor = asyncio.create_task(supervisor_task())
    collector = asyncio.create_task(metrics_collector_task())
    logger.info("Venvy is ready.")

    try:
        yield
    finally:
        logger.info("Venvy shutting down — stopping managed apps…")

        for task in (supervisor, collector):
            task.cancel()
        await asyncio.gather(supervisor, collector, return_exceptions=True)

        await _terminate_targets(*_collect_shutdown_targets())
        _mark_all_stopped()

        registry.clear_all()
        dispose_engine()
        logger.info("Venvy stopped.")
