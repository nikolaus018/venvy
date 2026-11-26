"""Which ports are an app's: one answer for every place where it matters.

Tunnel publishing, domains and routes (a hostname must only ever reach the
app's own port), the start of an app (is its port held by its own leftovers,
or by something else?), the supervisor's port scan, deploy health, the free
port search and System → Ports in use all ask here, so a docker or compose
app is never mistaken for a process again: its ports are the ones its
containers publish, which Docker listens on, not its CLI process.

- claimed(app):  the ports the app says are its own (its port, the ones set
                 by hand, the ones it was seen listening on).
- listening(app): what the app holds right now (blocking).
- holder(port, app): who holds a port now: None, "self" or "other" (blocking).
"""
import contextlib
import json
import socket
from typing import Dict, List, Optional, Set

from sqlmodel import Session, select

from venvy.models import App

CONTAINER_RUNTIMES = ("docker", "compose")


def is_container(app) -> bool:
    return getattr(app, "runtime", "") in CONTAINER_RUNTIMES


def _json_ports(raw) -> Set[int]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(value, list):
        return set()
    return {p for p in value if isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535}


def claimed(app, seen: bool = True) -> Set[int]:
    """The ports that are the app's: its port, the ones set by hand and (with
    *seen*) the ones it was last seen listening on."""
    ports = {int(app.port)} if getattr(app, "port", None) else set()
    ports |= _json_ports(getattr(app, "manual_ports", None))
    if seen:
        ports |= _json_ports(getattr(app, "detected_ports", None))
    return {p for p in ports if 1 <= p <= 65535}


def claimed_by_apps(session: Session, skip_app_id: Optional[int] = None) -> Dict[int, int]:
    """{port: app id} for every app's claimed ports. What a stopped app was last
    seen listening on is history, not its port, so only its set ports count."""
    out: Dict[int, int] = {}
    for a in session.exec(select(App)).all():
        if skip_app_id is not None and a.id == skip_app_id:
            continue
        for p in claimed(a, seen=a.status == "running"):
            out.setdefault(p, a.id)
    return out


def containers(app) -> Optional[List[dict]]:
    """The app's own running containers (docker inspect objects); None when it
    has none it could have (not a container app, or no docker). Blocking."""
    if not is_container(app):
        return None
    from venvy import process
    return process._own_containers_sync(app)


def listening(app) -> List[int]:
    """The TCP ports the app holds right now: what its process tree listens on
    and, for a docker / compose app, what its own containers publish. Blocking."""
    from venvy import process
    out: Set[int] = set()
    if getattr(app, "pid", None):
        with contextlib.suppress(Exception):
            if process._app_pid_alive(app):
                out |= set(process._detect_ports_sync(app.pid))
    if is_container(app):
        out |= set(process._container_host_ports_sync(app))
    return sorted(out)


def in_use(port: int) -> bool:
    """True if anything on the host listens on TCP *port*, on any interface and
    address family (or answers on it). Blocking."""
    from venvy import process
    if port in process._listening_ports():
        return True
    if not process._verify_port_free(port):
        return True
    lan_ip = process.get_server_ipv4()
    if lan_ip and lan_ip != "127.0.0.1":
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((lan_ip, port)) == 0:
                    return True
        except OSError:
            pass
    return False


def holder(port: int, app=None) -> Optional[str]:
    """Who holds *port* now: None (nothing listens), "self" (*app*'s own
    process tree or containers) or "other". Blocking."""
    if not in_use(port):
        return None
    if app is not None and port in listening(app):
        return "self"
    return "other"
