"""The hostnames Venvy itself is reached under.

Remembered from the Host header of every successful sign-in (and first-run
setup), so that nothing an operator configures for an app can take one of
them over: an app's own domain would put the app, not Venvy, behind every
request to that name (see proxy.py), and a tunnel route would point it at the
app's port — either way the next admin to sign in would be typing their
password into the app."""
import json
import logging
import re
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
_VALID_HOST_RE = re.compile(r"\A(?:[a-z0-9.\-]{1,253}|[0-9a-f:]{2,45})\Z")
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?")
_TLD_RE = re.compile(r"[a-z]{2,63}")


def valid_hostname(name: str, wildcard: bool = False) -> bool:
    """A lowercase DNS name with a dot (app.example.com), or with *wildcard*
    also *.example.com. Checked label by label: no pattern that backtracks."""
    if not name or len(name) > 253:
        return False
    labels = name.split(".")
    if wildcard and labels[0] == "*":
        labels = labels[1:]
    if len(labels) < 2 or not _TLD_RE.fullmatch(labels[-1]):
        return False
    return all(_LABEL_RE.fullmatch(lb) for lb in labels[:-1])

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
    if not name:
        return False
    known = names()
    if name in known:
        return True
    if name.startswith("*."):
        suffix = name[1:]
        return any(k.endswith(suffix) and k != suffix[1:] for k in known)
    return False


def remember(host: str) -> None:
    """Note that Venvy was signed in to under *host*. Best effort."""
    global _cache
    name = host_name(host)
    if not name or not _VALID_HOST_RE.fullmatch(name) or name in names():
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

