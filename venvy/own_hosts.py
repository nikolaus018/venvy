"""The hostnames Venvy itself is reached under.

Remembered from the Host header of every successful sign-in (and first-run
setup), so that nothing an operator configures for an app can take one of
them over: an app's own domain would put the app, not Venvy, behind every
request to that name (see proxy.py), and a tunnel route would point it at the
app's port — either way the next admin to sign in would be typing their
password into the app."""
import json
import logging
import threading
from typing import Optional, Set

from sqlmodel import Session

from venvy.db import engine
from venvy.models import SystemSetting

KEY = "venvy_hosts"
# Never forgotten once known, and no more taken in once full: if old names
# made way for new ones, anyone able to sign in could push the real one out
# with made-up Host headers and then claim it.
_MAX = 64

_lock = threading.Lock()
_cache: Optional[Set[str]] = None


def host_name(host: str) -> str:
    """The name part of a Host header value: no port, lower case, no trailing dot."""
    h = (host or "").strip().lower()
    if h.startswith("["):                       # [::1]:54783
        return h[1:h.find("]")] if "]" in h else h
    return h.split(":")[0].rstrip(".")


def names() -> Set[str]:
    global _cache
    with _lock:
        if _cache is None:
            loaded: Set[str] = set()
            try:
                with Session(engine) as s:
                    row = s.get(SystemSetting, KEY)
                if row and row.value:
                    data = json.loads(row.value)
                    if isinstance(data, list):
                        loaded = {str(n) for n in data if n}
            except Exception as exc:
                logging.warning(f"Could not read Venvy's own hostnames: {exc}")
                return set()
            _cache = loaded
        return set(_cache)


def is_own(host: str) -> bool:
    name = host_name(host)
    return bool(name) and name in names()


def remember(host: str) -> None:
    """Note that Venvy was signed in to under *host*. Best effort."""
    global _cache
    name = host_name(host)
    if not name or name in names():
        return
    try:
        with _lock, Session(engine) as s:
            row = s.get(SystemSetting, KEY) or SystemSetting(key=KEY)
            try:
                known = [n for n in json.loads(row.value or "[]") if n]
            except ValueError:
                known = []
            if name in known or len(known) >= _MAX:
                _cache = set(known)
                return
            known.append(name)
            row.value = json.dumps(known)
            s.add(row)
            s.commit()
            _cache = set(known)
    except Exception as exc:
        logging.warning(f"Could not remember hostname {name!r}: {exc}")

