"""Periodic sampling of per-app and host-wide resource usage.

Requires ``psutil``; without it the task exits immediately and the UI shows
zeroes rather than failing. Samples are cached in
:mod:`venvy.services.registry` so the API can answer instantly instead of
shelling out per request.
"""

from __future__ import annotations

import asyncio
import logging

from venvy.core.process_compat import psutil
from venvy.services import registry

__all__ = ["host_snapshot", "metrics_collector_task"]

logger = logging.getLogger(__name__)

#: Sampling interval. CPU percentages are computed between consecutive calls,
#: so this also sets the window each reading averages over.
_SAMPLE_INTERVAL_SECONDS = 2.5


async def metrics_collector_task() -> None:
    """Sample CPU and memory for every running app until cancelled."""
    if psutil is None:
        logger.info("psutil is not installed — per-app resource metrics are disabled.")
        return

    # psutil.Process objects must be reused between samples: cpu_percent() is
    # measured against the previous call on the *same* object, so a fresh one
    # each tick would always report 0.0.
    process_cache: dict[int, psutil.Process] = {}
    psutil.cpu_percent(interval=None)  # prime the host-wide counter
    logger.info("Metrics collector started.")

    try:
        while True:
            try:
                registry.set_host_cpu_percent(psutil.cpu_percent(interval=None))
                _evict_stale(process_cache)
                for app_id in list(registry.active_pids.keys()):
                    _sample_app(app_id, process_cache)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Metrics collection failed for this tick")
            await asyncio.sleep(_SAMPLE_INTERVAL_SECONDS)
    finally:
        logger.info("Metrics collector stopped.")


def _evict_stale(process_cache: dict[int, psutil.Process]) -> None:
    """Drop cached handles for processes that are no longer registered."""
    live = set(registry.active_pids.values())
    for pid in [pid for pid in process_cache if pid not in live]:
        process_cache.pop(pid, None)


def _sample_app(app_id: int, process_cache: dict[int, psutil.Process]) -> None:
    """Record one CPU/memory sample for *app_id*, including its children."""
    pid = registry.active_pids.get(app_id)
    if not pid:
        return
    try:
        process = process_cache.get(pid)
        if process is None:
            process = psutil.Process(pid)
            process.cpu_percent()  # prime this process's counter
            process_cache[pid] = process

        with process.oneshot():
            cpu = process.cpu_percent()
            memory = process.memory_info().rss

        # An app is usually a tree — a web server with workers, or cargo
        # wrapping the real binary — so the leader's own usage understates it.
        for child in process.children(recursive=True):
            try:
                with child.oneshot():
                    cpu += child.cpu_percent()
                    memory += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        registry.app_metrics[app_id] = {"cpu": round(cpu, 1), "memory": memory}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        registry.app_metrics.pop(app_id, None)
        process_cache.pop(pid, None)
    except Exception:
        logger.debug("Could not sample app %s", app_id, exc_info=True)


def host_snapshot() -> dict | None:
    """Return host-wide CPU, memory and disk usage, or None without psutil."""
    if psutil is None:
        return None

    import os

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = [0.0, 0.0, 0.0]

    return {
        "cpu": {
            "percent": registry.host_cpu_percent(),
            "load": load,
            "cores": psutil.cpu_count(logical=True),
        },
        "memory": {
            "total": memory.total,
            "available": memory.available,
            "used": memory.used,
            "percent": memory.percent,
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
        },
    }
