"""The background loop that keeps the database honest about what is running.

Responsibilities, once per tick:

* notice apps that died and mark them stopped,
* restart them when their policy says to,
* keep each running app's detected ports up to date,
* expire old sessions and rate-limit entries.

The loop wakes on a one-second timeout or immediately when
:func:`venvy.services.registry.trigger_supervisor` fires, so deliberate
lifecycle changes are reflected without waiting for the next tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from sqlmodel import select

from venvy.core.net import detect_listening_ports
from venvy.core.process_compat import pid_alive
from venvy.core.timeutils import make_tz_aware, utc_now
from venvy.db.engine import session_scope
from venvy.db.models import App, UserSession, require_id
from venvy.security import rate_limit
from venvy.services import registry
from venvy.services.processes import start_app

__all__ = ["supervisor_task"]

logger = logging.getLogger(__name__)

#: Longest the loop sleeps before checking again.
_TICK_SECONDS = 1.0
#: How often expired login sessions are purged.
_SESSION_CLEANUP_INTERVAL = 600.0
#: How often rate-limiter state is purged.
_RATE_LIMIT_CLEANUP_INTERVAL = 120.0
#: Minimum quiet period after an app stops before a restart policy may fire.
#: Stops a permanently-crashing app from becoming a busy loop.
_RESTART_BACKOFF_SECONDS = 5.0
#: How long to back off after an unexpected error in the loop body.
_ERROR_BACKOFF_SECONDS = 5.0


def _started_this_session(app: App) -> bool:
    """Return True if *app* was started by the current Venvy process.

    Apps left ``running`` in the database by a previous process are not
    automatically resurrected unless they are explicitly marked start-on-boot —
    otherwise an operator who stopped Venvy would find every app back up.
    """
    if app.last_start_time is None:
        return False
    return make_tz_aware(app.last_start_time) >= make_tz_aware(registry.BOOT_TIME)


def _stopped_by_operator(app: App) -> bool:
    """Return True if the most recent stop was a manual one.

    A manual stop outranks the restart policy: "always" must not undo a
    deliberate decision to take an app down.
    """
    if app.last_manually_stopped_at is None or app.last_start_time is None:
        return False
    return make_tz_aware(app.last_manually_stopped_at) >= make_tz_aware(app.last_start_time)


def _may_restart(app: App) -> bool:
    """Return True if the supervisor is allowed to restart *app* right now."""
    if app.restart_policy not in ("always", "on-failure"):
        return False
    if not _started_this_session(app) and not app.start_on_boot:
        return False
    return not _stopped_by_operator(app)


def _purge_expired_sessions() -> None:
    """Delete login sessions whose expiry has passed."""
    try:
        with session_scope() as session:
            expired = session.exec(
                select(UserSession).where(UserSession.expires_at < utc_now())
            ).all()
            for row in expired:
                session.delete(row)
            if expired:
                session.commit()
                logger.info("Purged %d expired session(s)", len(expired))
    except Exception as exc:
        logger.warning("Session cleanup failed: %s", exc)


def _mark_app_dead(app_id: int) -> None:
    """Record that *app_id*'s process is gone."""
    try:
        with session_scope() as session:
            app = session.get(App, app_id)
            if not app:
                return
            app.pid = None
            app.status = "stopped"
            app.last_stop_time = utc_now()
            if app.last_exit_code is None:
                # The reaper normally records the real code; reaching here means
                # the process vanished without us observing its exit.
                app.last_exit_code = 1
            session.add(app)
            session.commit()
    except Exception:
        logger.exception("Failed to mark app %s as stopped", app_id)
    registry.unregister_pid(app_id)


def _refresh_detected_ports(app: App) -> None:
    """Update *app*'s detected ports from what its process tree is listening on.

    Pinned ports win: when the user has set ``manual_ports`` the observation is
    ignored, because they have told us which port is authoritative.
    """
    try:
        manual: list[int] = json.loads(app.manual_ports or "[]")
    except (ValueError, TypeError):
        manual = []

    if manual:
        combined = manual
    else:
        combined = detect_listening_ports(app.pid) if app.pid else []

    encoded = json.dumps(combined)
    if app.detected_ports == encoded:
        return

    with session_scope() as session:
        refreshed = session.get(App, app.id)
        if not refreshed:
            return
        refreshed.detected_ports = encoded
        if not refreshed.port and combined:
            refreshed.port = combined[0]
        session.add(refreshed)
        session.commit()
    logger.info("Ports for '%s' updated to %s", app.name, combined)


async def _restart(app: App) -> None:
    """Restart *app*, logging but swallowing failures."""
    logger.info("Restarting '%s' (policy: %s)", app.name, app.restart_policy)
    app_id = require_id(app.id)
    try:
        with session_scope() as session:
            await start_app(app_id, session)
    except Exception as exc:
        logger.error("Restart failed for '%s': %s", app.name, exc)


async def _reconcile_app(app: App) -> None:
    """Bring the database in line with reality for a single app."""
    app_id = require_id(app.id)
    is_alive = bool(app.pid and pid_alive(app.pid))

    if app.status == "running" and not is_alive:
        logger.warning("App '%s' (PID %s) died unexpectedly.", app.name, app.pid)
        _mark_app_dead(app_id)
        if _may_restart(app):
            await _restart(app)
        return

    if app.status == "running" and is_alive:
        try:
            await asyncio.to_thread(_refresh_detected_ports, app)
        except Exception as exc:
            logger.debug("Port scan failed for '%s': %s", app.name, exc)
        return

    # Stopped, but a restart policy may still apply — this covers an app that
    # was reaped by the exit handler rather than found dead here.
    if app.status != "stopped" or app.last_start_time is None:
        return
    if app.last_stop_time is not None:
        since_stop = (utc_now() - make_tz_aware(app.last_stop_time)).total_seconds()
        if since_stop < _RESTART_BACKOFF_SECONDS:
            return

    wants_restart = app.restart_policy == "always" or (
        app.restart_policy == "on-failure" and app.last_exit_code not in (0, None)
    )
    if wants_restart and _may_restart(app):
        await _restart(app)


async def supervisor_task() -> None:
    """Run the supervision loop until cancelled."""
    registry.set_supervisor_event(asyncio.Event())
    last_session_cleanup = 0.0
    last_rate_limit_cleanup = 0.0
    logger.info("Supervisor started.")

    try:
        while True:
            try:
                await registry.wait_for_supervisor_signal(_TICK_SECONDS)

                now = time.monotonic()
                if now - last_session_cleanup > _SESSION_CLEANUP_INTERVAL:
                    last_session_cleanup = now
                    await asyncio.to_thread(_purge_expired_sessions)
                if now - last_rate_limit_cleanup > _RATE_LIMIT_CLEANUP_INTERVAL:
                    last_rate_limit_cleanup = now
                    rate_limit.purge_expired()

                try:
                    with session_scope() as session:
                        apps = list(session.exec(select(App)).all())
                except Exception as exc:
                    logger.error("Supervisor could not read the app list: %s", exc)
                    await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
                    continue

                for app in apps:
                    # Transient states belong to an in-flight API call; acting
                    # on them would race with the request that set them.
                    if app.status in ("starting", "stopping"):
                        continue
                    await _reconcile_app(app)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in the supervisor loop")
                await asyncio.sleep(_ERROR_BACKOFF_SECONDS)
    finally:
        registry.set_supervisor_event(None)
        logger.info("Supervisor stopped.")
