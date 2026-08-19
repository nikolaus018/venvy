"""Network introspection: free ports, listening sockets, and the host's LAN IP.

Port detection has two backends. When ``psutil`` is installed it is used
directly; otherwise Venvy reads ``/proc`` itself, matching socket inodes from
``/proc/<pid>/fd`` against ``/proc/net/tcp``. The fallback keeps the feature
working on a minimal container image with no compiler to build psutil.
"""

from __future__ import annotations

import logging
import os
import socket
from pathlib import Path

from venvy.core.process_compat import psutil

__all__ = [
    "descendant_pids",
    "detect_listening_ports",
    "find_free_port",
    "get_server_ipv4",
    "is_port_free",
    "list_listening_sockets",
    "suggest_free_ports",
]

logger = logging.getLogger(__name__)

#: Hex-encoded TCP state for LISTEN in /proc/net/tcp.
_TCP_LISTEN = "0A"

#: Ranges the UI offers when suggesting a port for a new app — the conventional
#: dev-server ports developers already recognise.
_SUGGESTION_RANGES = (
    (3000, 3010),
    (4000, 4010),
    (5000, 5010),
    (8000, 8020),
    (8080, 8090),
    (8888, 8898),
    (9000, 9010),
    (9090, 9100),
)


def find_free_port(start: int = 8001, end: int = 9000) -> int | None:
    """Return the first bindable TCP port in ``[start, end)``, or None."""
    for port in range(start, end):
        if _can_bind(port):
            return port
    return None


def _can_bind(port: int) -> bool:
    """Return True if this process can bind *port* on all interfaces right now."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Binding all interfaces is the point: it answers "can an app
            # listen here?", which a loopback-only bind would not.
            sock.bind(("0.0.0.0", port))  # noqa: S104
            return True
    except OSError:
        return False


def is_port_free(port: int) -> bool:
    """Return True if nothing is currently accepting connections on *port*.

    Checked immediately before launching an app: a port that was free when the
    app was configured may have been taken since.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def get_server_ipv4() -> str:
    """Return this host's LAN IPv4 address, best-effort.

    Opens an unconnected UDP socket toward a public address purely to ask the
    routing table which local interface would be used; no packets are sent.
    Falls back to loopback when the host has no route out.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def _net_connections(kind: str = "tcp") -> list:
    """Call ``psutil.net_connections`` defensively.

    The signature changed in psutil 6.0 and the call raises ``AccessDenied`` on
    hosts where it cannot read other users' sockets, so failures degrade to an
    empty list and let the ``/proc`` path take over.
    """
    if psutil is None:
        return []
    try:
        return list(psutil.net_connections(kind=kind))
    except Exception:
        return []


def descendant_pids(root_pid: int) -> list[int]:
    """Return *root_pid* and every descendant process id."""
    pids = [root_pid]
    if psutil is not None:
        try:
            pids += [child.pid for child in psutil.Process(root_pid).children(recursive=True)]
            return list(dict.fromkeys(pids))
        except Exception:
            pass

    # /proc fallback: build the parent → children map once, then walk it.
    children: dict[int, list[int]] = {}
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                ppid = int((entry / "stat").read_text().split()[3])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry.name))
    except OSError:
        return pids

    queue, seen = [root_pid], {root_pid}
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in seen:
                seen.add(child)
                pids.append(child)
                queue.append(child)
    return pids


def _listening_ports_via_proc(pids: list[int]) -> set[int]:
    """Find listening ports for *pids* by matching socket inodes in /proc."""
    ports: set[int] = set()
    for pid in pids:
        inodes: set[str] = set()
        try:
            for fd_path in Path(f"/proc/{pid}/fd").iterdir():
                try:
                    # os.readlink over Path.readlink: this loop runs over every fd
                    # of every process, so it stays on the cheaper call.
                    link = os.readlink(str(fd_path))  # noqa: PTH115
                except OSError:
                    continue
                if link.startswith("socket:["):
                    inodes.add(link[8:-1])
        except OSError:
            continue
        if not inodes:
            continue

        for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(tcp_file).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 10 or parts[3] != _TCP_LISTEN or parts[9] not in inodes:
                    continue
                try:
                    port = int(parts[1].split(":")[1], 16)
                except (ValueError, IndexError):
                    continue
                if port > 0:
                    ports.add(port)
    return ports


def detect_listening_ports(app_pid: int) -> list[int]:
    """Return every TCP port the process tree rooted at *app_pid* listens on.

    This is how Venvy learns which port an app actually chose when the user did
    not pin one — the app is asked by observation rather than by convention.
    """
    pids = descendant_pids(app_pid)
    ports = _listening_ports_via_proc(pids)

    if not ports and psutil is not None:
        pid_set = set(pids)
        for conn in _net_connections("tcp"):
            if (
                conn.status == "LISTEN"
                and conn.laddr
                and conn.pid in pid_set
                and conn.laddr.port > 0
            ):
                ports.add(conn.laddr.port)

    return sorted(ports)


def list_listening_sockets() -> list[dict]:
    """Return ``{"port", "pid"}`` for every listening TCP socket on the host."""
    listening: list[dict] = []
    if psutil is not None:
        for conn in _net_connections("tcp"):
            if conn.status == "LISTEN" and conn.laddr:
                listening.append({"port": conn.laddr.port, "pid": conn.pid})
        if listening:
            return listening

    for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(tcp_file).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != _TCP_LISTEN:
                continue
            try:
                listening.append({"port": int(parts[1].split(":")[1], 16), "pid": None})
            except (ValueError, IndexError):
                continue
    return listening


def suggest_free_ports(used: set[int], count: int = 12) -> list[int]:
    """Return up to *count* bindable ports from the conventional dev ranges."""
    free: list[int] = []
    for start, end in _SUGGESTION_RANGES:
        for port in range(start, end):
            if len(free) >= count:
                return free
            if port not in used and _can_bind(port):
                free.append(port)
    return free
