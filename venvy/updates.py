"""Updating Venvy from its own UI.

Venvy cannot update itself: an update replaces its code and restarts its
service, which takes root, and Venvy usually runs as an unprivileged user. The
installer sets up the privileged half instead — a systemd path unit that waits
for REQUEST_FILE to appear in the data directory and then runs
/opt/venvy/update.sh as root. That script downloads the latest release, checks
its checksum and runs the installer that came with it, which switches over and
goes back to the previous version by itself if the new one does not come up.

Venvy only ever creates the (empty) request file. Nothing it writes reaches
the root side, so the most a compromised Venvy could do through this is ask
for the official latest release. The update runs in its own systemd unit, not
under Venvy, so it carries on while the installer stops and restarts Venvy.
"""
import asyncio
import datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple

import aiohttp

from venvy import __version__
from venvy.config import ASSETS_DIR, DATA_DIR

REQUEST_FILE = DATA_DIR / ".update-request"
# Written by update.sh (root) in a directory only root can write; Venvy reads it.
UPDATE_DIR = Path(os.getenv("VENVY_UPDATE_DIR", "/opt/venvy/update"))
STATUS_FILE = UPDATE_DIR / "status.json"
LOG_FILE = UPDATE_DIR / "update.log"
PATH_UNIT = Path("/etc/systemd/system/venvy-update.path")
# Where releases come from; the installer puts it in the service's environment.
RELEASE_URL = os.getenv("VENVY_RELEASE_URL", "").rstrip("/")

STALE_SEC = 45 * 60            # a "running" update older than this was cut short
CHECK_TTL_SEC = 3600.0
_VERSION_RE = re.compile(r"v?\d+(\.\d+){1,3}([.\-][0-9A-Za-z.\-]+)?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_checked: dict = {"at": 0.0, "version": None, "error": None, "when": None}
_check_lock = asyncio.Lock()


def current_version() -> str:
    """The running release's tag (its VERSION file), or the package version in a checkout."""
    try:
        v = (ASSETS_DIR / "VERSION").read_text().strip()
        if _VERSION_RE.fullmatch(v):
            return v
    except OSError:
        pass
    return f"v{__version__}"


def _order(version: str) -> tuple:
    """Sort key for a tag: the numbers first; then a release ranks above its
    pre-releases (v1.2.0 > v1.2.0-rc2), and suffixes compare naturally (ci10 > ci9)."""
    m = re.match(r"v?(\d+(?:\.\d+)*)(.*)", version or "")
    if not m:
        return ((), 0, ())
    numbers = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2).lstrip(".-")
    parts = tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.findall(r"\d+|[A-Za-z]+", suffix))
    return (numbers, 0 if suffix else 1, parts)


def is_newer(candidate: Optional[str], than: str) -> bool:
    return bool(candidate) and _order(candidate) > _order(than)


def unsupported_reason() -> Optional[str]:
    """Why this Venvy cannot update itself, or None when it can."""
    if not RELEASE_URL or not PATH_UNIT.exists():
        return ("Updating from here needs a Venvy set up by the installer (4.5.0 or later). "
                "Run the install command on the server once; after that, updates can start here.")
    return None


async def latest_version(force: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """(the newest release's tag, error message), checked at most once an hour."""
    if not RELEASE_URL:
        return None, None
    async with _check_lock:
        if not force and time.monotonic() - _checked["at"] < CHECK_TTL_SEC and _checked["at"]:
            return _checked["version"], _checked["error"]
        version, error = None, None
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(f"{RELEASE_URL}/VERSION") as resp:
                    if resp.status != 200:
                        error = f"The release server answered {resp.status}."
                    else:
                        text = (await resp.text()).strip()
                        if _VERSION_RE.fullmatch(text):
                            version = text
                        else:
                            error = "The release server did not say which version is the latest."
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            error = f"Could not reach the release server: {exc.__class__.__name__}"
        _checked.update(at=time.monotonic(), version=version, error=error,
                        when=datetime.datetime.now(datetime.timezone.utc).isoformat())
        return version, error


_notes_cache: dict = {"at": 0.0, "sections": None, "error": None}
_NOTES_MAX_BYTES = 512 * 1024


def parse_changelog(text: str) -> list:
    """[{"version", "items": [str, ...]}] from CHANGELOG.md: one "## vX.Y.Z" heading per
    release, its points as "- " lines (a line that does not start a point continues the last one)."""
    sections, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            version = line[3:].strip()
            cur = {"version": version, "items": []} if _VERSION_RE.fullmatch(version) else None
            if cur:
                sections.append(cur)
        elif cur is not None and line.strip():
            stripped = line.strip()
            if stripped[:2] in ("- ", "* "):
                cur["items"].append(stripped[2:].strip())
            elif cur["items"]:
                cur["items"][-1] += " " + stripped
            else:
                cur["items"].append(stripped)
    return sections


async def release_notes(current: str, force: bool = False) -> Tuple[list, Optional[str]]:
    """(the changelog sections newer than *current* — or the latest one when there are
    none — and an error message). The changelog is fetched at most once an hour."""
    if not RELEASE_URL:
        return [], None
    async with _check_lock:
        if force or not _notes_cache["at"] or time.monotonic() - _notes_cache["at"] >= CHECK_TTL_SEC:
            sections, error = None, None
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(f"{RELEASE_URL}/CHANGELOG.md") as resp:
                        if resp.status != 200:
                            error = f"The release server answered {resp.status}."
                        else:
                            raw = await resp.content.read(_NOTES_MAX_BYTES)
                            sections = parse_changelog(raw.decode(errors="replace"))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                error = f"Could not reach the release server: {exc.__class__.__name__}"
            _notes_cache.update(at=time.monotonic(), sections=sections, error=error)
    sections = _notes_cache["sections"] or []
    newer = [sec for sec in sections if is_newer(sec["version"], current)]
    return (newer or sections[:1])[:30], _notes_cache["error"]


def last_checked() -> Optional[str]:
    """When the newest release was last looked up (ISO time), or None if not yet."""
    return _checked["when"]


def _read_status() -> dict:
    try:
        data = json.loads(STATUS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _log_tail(lines: int = 80) -> list:
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    return [_ANSI_RE.sub("", ln) for ln in text.splitlines()[-lines:]]


def state() -> dict:
    """How the last (or current) update went, as update.sh reports it."""
    st = _read_status()
    state_name = st.get("state") if st.get("state") in ("running", "ok", "failed") else None
    if state_name == "running":
        try:
            age = time.time() - STATUS_FILE.stat().st_mtime
        except OSError:
            age = 0
        if age > STALE_SEC:
            state_name = "failed"          # cut short (a reboot, say): do not block the next one
    return {
        "state": "requested" if REQUEST_FILE.exists() and state_name != "running" else state_name,
        "started": st.get("started"), "finished": st.get("finished"),
        "log": _log_tail() if state_name else [],
    }


def busy() -> bool:
    return state()["state"] in ("requested", "running")


def request() -> None:
    """Ask the root side for an update. Raises FileExistsError if one is already asked for."""
    fd = os.open(REQUEST_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
