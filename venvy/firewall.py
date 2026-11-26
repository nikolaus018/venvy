"""Keeping apps behind Venvy, with nftables.

Venvy owns one table, ``inet venvy``, and rewrites it whenever apps change:

* Private apps (bind_public off): their ports accept connections from this
  host only (loopback), so they are reached through Venvy's proxy or the
  tunnel and not directly from the network — even when the app binds 0.0.0.0.
* App-to-app isolation (per-app users): a hosted app's user may connect to
  its own ports and to the services it is bound to, and to nothing else of
  another app's on this host; other software (Venvy, cloudflared, Caddy) is
  not affected.

Nothing is touched when nft is not available or Venvy is not root; the
System view says so. Docker publishes ports through its own NAT rules, which
is why docker apps are published on 127.0.0.1 unless they are public.
"""
import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
from typing import Dict, List, Optional, Set

from sqlmodel import Session, select

from venvy.db import engine
from venvy.models import App, ServiceBinding

TABLE = "venvy"
ENABLED = os.getenv("VENVY_FIREWALL", "1") == "1"
_last: Optional[str] = None
_status = {"active": False, "reason": "not checked yet", "rules": 0}
_lock: Optional[asyncio.Lock] = None


def _nft() -> Optional[str]:
    return shutil.which("nft", path="/usr/sbin:/usr/bin:/sbin:/bin")


def usable() -> Optional[str]:
    """None if the firewall can be managed, else why not."""
    if not ENABLED:
        return "turned off (VENVY_FIREWALL=0)"
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return "Venvy is not running as root"
    if not _nft():
        return "nft is not installed (apt-get install nftables)"
    return None


def status() -> dict:
    return dict(_status)


def _ports(a: App) -> List[int]:
    from venvy import ports
    return sorted(ports.claimed(a))


def ruleset() -> str:
    """The table as nft script text (empty table when nothing is to be done)."""
    from venvy.isolation import _app_ids, _isolation_active
    with Session(engine) as s:
        apps = list(s.exec(select(App)).all())
        bindings = list(s.exec(select(ServiceBinding)).all())
    owner = {a.id: (a.parent_app_id or a.id) for a in apps}
    by_owner: Dict[int, Set[int]] = {}
    private: Set[int] = set()
    for a in apps:
        ports = _ports(a)
        by_owner.setdefault(owner[a.id], set()).update(ports)
        root = next((x for x in apps if x.id == owner[a.id]), a)
        if not root.bind_public:
            private.update(ports)
    lines = [f"table inet {TABLE} {{"]
    lines.append("  chain input {")
    lines.append("    type filter hook input priority -5; policy accept;")
    if private:
        lines.append(f"    iifname != \"lo\" tcp dport {{ {', '.join(map(str, sorted(private)))} }} drop")
        lines.append(f"    iifname != \"lo\" udp dport {{ {', '.join(map(str, sorted(private)))} }} drop")
    lines.append("  }")
    lines.append("  chain output {")
    lines.append("    type filter hook output priority -5; policy accept;")
    if _isolation_active():
        uids: Dict[int, int] = {}
        for oid in by_owner:
            row = next((x for x in apps if x.id == oid), None)
            ids = _app_ids(row) if row is not None else None
            if ids:
                uids[oid] = ids[0]
        allowed: Dict[int, Set[int]] = {oid: {oid} for oid in by_owner}
        for b in bindings:
            allowed.setdefault(owner.get(b.service_app_id, b.service_app_id), set()).add(owner.get(b.app_id, b.app_id))
        for oid, ports in sorted(by_owner.items()):
            if not ports:
                continue
            others = sorted(uid for o, uid in uids.items() if o not in allowed.get(oid, {oid}))
            if not others:
                continue
            lines.append(f"    oifname \"lo\" meta skuid {{ {', '.join(map(str, others))} }} "
                         f"tcp dport {{ {', '.join(map(str, sorted(ports)))} }} reject with tcp reset")
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _apply_sync(text: str) -> None:
    nft = _nft()
    assert nft
    # Replace the table in one transaction: delete (if there) and add.
    script = f"table inet {TABLE}\ndelete table inet {TABLE}\n" + text
    r = subprocess.run([nft, "-f", "-"], input=script.encode(), capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-500:])


async def apply() -> dict:
    """Bring the table up to date (cheap when nothing changed)."""
    global _last, _lock
    why = usable()
    if why:
        _status.update(active=False, reason=why)
        return status()
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        try:
            text = await asyncio.to_thread(ruleset)
            if text != _last:
                await asyncio.to_thread(_apply_sync, text)
                _last = text
            _status.update(active=True, reason="", rules=text.count(" drop") + text.count(" reject"))
        except Exception as exc:
            _status.update(active=False, reason=f"nft failed: {exc}")
            logging.warning("Firewall update failed: %s", exc)
    return status()


async def firewall_task() -> None:
    while True:
        with contextlib.suppress(Exception):
            await apply()
        await asyncio.sleep(10)


def remove() -> None:
    nft = _nft()
    if nft and usable() is None:
        subprocess.run([nft, "delete", "table", "inet", TABLE], capture_output=True, timeout=30)
