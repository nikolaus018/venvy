"""In-memory registry of what this Venvy process is currently running.

The database records what *should* be running; this module records what this
process actually launched. Keeping both matters because the two can disagree —
an app can die between polls, or a stale row can survive a hard restart — and
shutdown needs the authoritative list of PIDs it is responsible for reaping.

All state is process-local and is intentionally lost on restart; the startup
sequence reconciles the database against reality instead.
"""

from __future__ import annotations

import asyncio
import logging

from venvy.core.timeutils import utc_now as _utc_now

__all__ = [
    "BOOT_TIME",
    "active_pids",
    "app_metrics",
    "cancel_log_task",
    "clear_all",
    "host_cpu_percent",
    "register_pid",
    "set_host_cpu_percent",
    "set_log_task",
    "set_supervisor_event",
    "track_task",
    "trigger_supervisor",
    "unregister_pid",
    "wait_for_supervisor_signal",
]

logger = logging.getLogger(__name__)

#: When this Venvy process started. The supervisor compares an app's
#: ``last_start_time`` against this to tell "started during this session" from
#: "left over in the database from a previous one".
BOOT_TIME = _utc_now()

#: PIDs launched in this session, keyed by app id. Survives DB desync.
active_pids: dict[int, int] = {}

#: Latest sampled resource usage per app id: ``{"cpu": float, "memory": int}``.
app_metrics: dict[int, dict[str, float | int]] = {}

#: Log-tailing task per app id, so a restart can cancel the previous tail.
_log_tasks: dict[int, asyncio.Task] = {}

#: Strong references to fire-and-forget tasks. Without this the event loop only
#: holds weak references and a task can be garbage-collected mid-flight.
_background_tasks: set[asyncio.Task] = set()

_host_cpu_percent: float = 0.0
_supervisor_event: asyncio.Event | None = None


def host_cpu_percent() -> float:
    """Return the most recent host-wide CPU utilisation sample."""
    return _host_cpu_percent


def set_host_cpu_percent(value: float) -> None:
    """Record a host-wide CPU utilisation sample."""
    global _host_cpu_percent
    _host_cpu_percent = value


def register_pid(app_id: int, pid: int) -> None:
    """Record that *app_id* is running as *pid*."""
    active_pids[app_id] = pid


def unregister_pid(app_id: int) -> None:
    """Forget *app_id*'s PID and its last metrics sample."""
    active_pids.pop(app_id, None)
    app_metrics.pop(app_id, None)


def track_task(task: asyncio.Task) -> asyncio.Task:
    """Hold a strong reference to *task* until it finishes."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def set_log_task(app_id: int, task: asyncio.Task) -> None:
    """Register the log-tailing task for *app_id*, replacing any previous one."""
    cancel_log_task(app_id)
    _log_tasks[app_id] = task


def cancel_log_task(app_id: int) -> None:
    """Cancel and forget *app_id*'s log tail, if one is running."""
    task = _log_tasks.pop(app_id, None)
    if task and not task.done():
        task.cancel()


def cancel_all_log_tasks() -> None:
    """Cancel every log tail. Called during shutdown."""
    for task in list(_log_tasks.values()):
        if not task.done():
            task.cancel()
    _log_tasks.clear()


def set_supervisor_event(event: asyncio.Event | None) -> None:
    """Install the event the supervisor loop waits on."""
    global _supervisor_event
    _supervisor_event = event


def trigger_supervisor() -> None:
    """Wake the supervisor immediately instead of waiting for its next tick.

    Called after any deliberate lifecycle change so the UI reflects it at once.
    """
    if _supervisor_event is not None:
        _supervisor_event.set()


async def wait_for_supervisor_signal(timeout: float) -> None:
    """Block until :func:`trigger_supervisor` fires or *timeout* elapses."""
    if _supervisor_event is None:
        await asyncio.sleep(timeout)
        return
    try:
        await asyncio.wait_for(_supervisor_event.wait(), timeout=timeout)
    except TimeoutError:
        pass
    finally:
        _supervisor_event.clear()


def clear_all() -> None:
    """Reset every registry. For tests and shutdown."""
    active_pids.clear()
    app_metrics.clear()
    cancel_all_log_tasks()
    _background_tasks.clear()
