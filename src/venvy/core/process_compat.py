"""Process inspection that works with or without ``psutil``.

``psutil`` is an optional dependency: it gives richer and faster introspection,
but requires a build toolchain on some platforms. Every function here falls
back to ``os`` and ``/proc`` so that Venvy keeps working without it — only the
per-app CPU/memory graphs go dark.

Import ``psutil`` from this module rather than directly, so the "is it
available?" question is answered in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

__all__ = [
    "HAS_PROCESS_GROUPS",
    "HAS_PSUTIL",
    "get_pgid",
    "own_pgid",
    "pid_alive",
    "pid_alive_async",
    "psutil",
    "signal_group",
    "signal_pid",
    "systemd_available",
]

logger = logging.getLogger(__name__)

try:  # pragma: no cover - presence depends on the host environment
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

#: Whether richer process introspection is available.
HAS_PSUTIL = psutil is not None

#: Whether this platform has POSIX process groups. Windows has neither
#: ``os.getpgid`` nor ``os.killpg``, so group-wide teardown is unavailable and
#: callers fall back to signalling individual processes.
HAS_PROCESS_GROUPS = hasattr(os, "getpgid") and hasattr(os, "killpg")


def get_pgid(pid: int) -> int | None:
    """Return *pid*'s process-group id, or None if unavailable."""
    if not HAS_PROCESS_GROUPS:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def own_pgid() -> int | None:
    """Return this process's own group id, or None if unavailable.

    Callers compare against it before signalling a group: sending SIGKILL to
    Venvy's own process group would take Venvy down with the app.
    """
    return get_pgid(0)


def signal_group(pgid: int, sig: int) -> bool:
    """Signal a whole process group. Returns False if it could not be delivered.

    Signal 0 delivers nothing and only probes whether the group still exists.
    """
    if not HAS_PROCESS_GROUPS:
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except OSError:
        return False


def signal_pid(pid: int, sig: int) -> bool:
    """Signal one process. Returns False if it was already gone or denied."""
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def pid_alive(pid: int) -> bool:
    """Return True if *pid* exists and is not a zombie.

    A zombie is deliberately reported as dead: the process has exited and only
    its exit status is still held, so treating it as alive would stop the
    supervisor from restarting a crashed app.
    """
    if psutil is not None:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            # It exists, we just cannot introspect it (different user).
            return True
        except Exception:
            pass  # fall through to the signal-based probe

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # EPERM — exists but owned by another user
    except ProcessLookupError:
        return False  # ESRCH — no such process
    except OSError:
        return False


async def pid_alive_async(pid: int) -> bool:
    """Non-blocking :func:`pid_alive`, for use on the event loop."""
    return await asyncio.to_thread(pid_alive, pid)


_systemd_available: bool | None = None


def systemd_available() -> bool:
    """Return True if apps can be launched under a transient systemd scope.

    Requires both binaries *and* systemd actually being PID 1 — the tools are
    present in many container images where the manager is not running. Cached,
    since the answer cannot change while the process is alive.
    """
    global _systemd_available
    if _systemd_available is None:
        _systemd_available = bool(
            shutil.which("systemd-run")
            and shutil.which("systemctl")
            and Path("/run/systemd/system").exists()
        )
        if not _systemd_available:
            logger.warning(
                "systemd-run is unavailable — apps will start without enforced CPU/memory "
                "limits. This is expected on non-systemd hosts and in most containers."
            )
    return _systemd_available
