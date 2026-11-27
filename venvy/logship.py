"""Sending app logs elsewhere, as they are written.

Targets (System → Logs), any number of them:

    syslog   RFC 5424 over UDP or TCP to host:port (rsyslog, Graylog, Papertrail …)
    journald the local journal, through /dev/log (journalctl -t venvy-app-<id>)
    loki     Grafana Loki's push API (labels: app, app_id, process, host)
    http     JSON lines POSTed to any URL (Vector, Fluent Bit, Logstash, a SIEM)

Lines are queued and sent in batches by one background task; a target that
is down drops what it can't take (counted, reported in System → Logs) rather
than holding up the apps. Credentials (a Loki token, HTTP headers) are stored
encrypted.
"""
import asyncio
import contextlib
import datetime
import json
import re
import socket
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp

from venvy import kv
from venvy.config import HTTP_CLIENT_LIMITS

KEY = "log_targets"
KINDS = ("syslog", "journald", "loki", "http")
_QUEUE_MAX = 20000
_queue: Optional[asyncio.Queue] = None
_registry: Dict[str, Tuple[int, str, str]] = {}      # log file -> (app id, app name, process)
_targets: List[dict] = []
_stats: Dict[str, dict] = {}
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
_HOST = socket.gethostname()


def register(log_file: Path, app_id: int, name: str, process: str = "web") -> None:
    _registry[str(log_file)] = (app_id, name, process)


def load_targets() -> List[dict]:
    return kv.get_json(KEY, [], secret=True) or []


def masked() -> List[dict]:
    out = []
    for i, t in enumerate(load_targets()):
        m = {k: v for k, v in t.items() if k not in ("token", "headers", "password")}
        m["has_token"] = bool(t.get("token") or t.get("headers"))
        m["id"] = i
        m["stats"] = _stats.get(_tkey(t), {})
        out.append(m)
    return out


def _tkey(t: dict) -> str:
    return f"{t.get('kind')}|{t.get('url') or t.get('host')}|{t.get('port')}"


def clean_target(t: dict) -> dict:
    from venvy.notify import _check_safe_outbound_host, _check_safe_outbound_url
    kind = t.get("kind")
    if kind not in KINDS:
        raise ValueError("kind must be one of " + ", ".join(KINDS))
    out: dict = {"kind": kind, "enabled": bool(t.get("enabled", True))}
    if kind == "syslog":
        host = str(t.get("host") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,253}", host):
            raise ValueError("syslog needs a host name or address.")
        _check_safe_outbound_host(host.strip("[]"), "host")
        port = int(t.get("port") or 514)
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1-65535.")
        proto = t.get("protocol") or "udp"
        if proto not in ("udp", "tcp"):
            raise ValueError("protocol must be udp or tcp.")
        out.update(host=host, port=port, protocol=proto)
    elif kind in ("loki", "http"):
        url = str(t.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError("url must be http(s)://")
        _check_safe_outbound_url(url, "url")
        out["url"] = url
        if t.get("token"):
            out["token"] = str(t["token"])
        if t.get("username"):
            out["username"] = str(t["username"])
            out["password"] = str(t.get("password") or "")
    apps = t.get("apps")
    if apps:
        out["apps"] = [int(a) for a in apps]
    return out


def save_targets(targets: List[dict]) -> None:
    kv.put_json(KEY, targets, secret=True)
    reload()


def reload() -> None:
    global _targets
    _targets = [t for t in load_targets() if t.get("enabled", True)]


def offer(log_file: Path, lines: List[bytes], when: Optional[datetime.datetime] = None) -> None:
    """Called for every batch of lines written to an app's log (never blocks)."""
    if not _targets or _queue is None:
        return
    meta = _registry.get(str(log_file))
    if meta is None:
        return
    ts = (when or datetime.datetime.now(datetime.timezone.utc)).timestamp()
    for ln in lines:
        if _queue.full():
            for t in _targets:
                _stats.setdefault(_tkey(t), {}).setdefault("dropped", 0)
                _stats[_tkey(t)]["dropped"] += 1
            return
        _queue.put_nowait((ts, meta, _ANSI.sub(b"", ln).decode("utf-8", errors="replace")))


def _rfc5424(ts: float, meta, line: str) -> bytes:
    stamp = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    app_id, name, proc = meta
    return f"<14>1 {stamp} {_HOST} venvy-app-{app_id} - {proc} - {line}".encode("utf-8", errors="replace")


async def _send_syslog(t: dict, batch) -> None:
    if t["protocol"] == "udp":
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol,
                                                           remote_addr=(t["host"].strip("[]"), t["port"]))
        try:
            for ts, meta, line in batch:
                transport.sendto(_rfc5424(ts, meta, line)[:8000])
        finally:
            transport.close()
    else:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(t["host"].strip("[]"), t["port"]), 10)
        try:
            for ts, meta, line in batch:
                msg = _rfc5424(ts, meta, line)
                writer.write(f"{len(msg)} ".encode() + msg)       # octet counting, RFC 6587
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _send_journald(batch) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.connect("/dev/log")
        for _ts, meta, line in batch:
            s.send(f"<14>venvy-app-{meta[0]}: {line}".encode("utf-8", errors="replace")[:8000])
    finally:
        s.close()


async def _send_http(http, t: dict, batch) -> None:
    headers = {"Content-Type": "application/json"}
    auth = None
    if t.get("token"):
        headers["Authorization"] = f"Bearer {t['token']}"
    if t.get("username"):
        auth = aiohttp.BasicAuth(t["username"], t.get("password", ""))
    if t["kind"] == "loki":
        streams: Dict[tuple, list] = {}
        for ts, meta, line in batch:
            streams.setdefault(meta, []).append([str(int(ts * 1e9)), line])
        body = {"streams": [{"stream": {"app": m[1], "app_id": str(m[0]), "process": m[2], "host": _HOST,
                                        "source": "venvy"}, "values": v} for m, v in streams.items()]}
        url = t["url"].rstrip("/")
        if not url.endswith("/loki/api/v1/push"):
            url += "/loki/api/v1/push"
    else:
        body = [{"ts": datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(), "app": m[1],
                 "app_id": m[0], "process": m[2], "host": _HOST, "line": line} for ts, m, line in batch]
        url = t["url"]
    async with http.post(url, data=json.dumps(body), headers=headers, auth=auth) as r:
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")


async def shipper_task() -> None:
    global _queue
    _queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    reload()
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), **HTTP_CLIENT_LIMITS)
    try:
        while True:
            first = await _queue.get()
            batch = [first]
            end = time.monotonic() + 1.0
            while len(batch) < 1000 and time.monotonic() < end:
                try:
                    batch.append(await asyncio.wait_for(_queue.get(), max(0.01, end - time.monotonic())))
                except asyncio.TimeoutError:
                    break
            for t in list(_targets):
                sub = [b for b in batch if not t.get("apps") or b[1][0] in t["apps"]]
                if not sub:
                    continue
                st = _stats.setdefault(_tkey(t), {"sent": 0, "errors": 0, "dropped": 0, "last_error": ""})
                try:
                    if t["kind"] == "syslog":
                        await _send_syslog(t, sub)
                    elif t["kind"] == "journald":
                        await asyncio.to_thread(_send_journald, sub)
                    else:
                        await _send_http(http, t, sub)
                    st["sent"] = st.get("sent", 0) + len(sub)
                except Exception as exc:
                    st["errors"] = st.get("errors", 0) + 1
                    st["dropped"] = st.get("dropped", 0) + len(sub)
                    st["last_error"] = str(exc)[:200] or exc.__class__.__name__
    except asyncio.CancelledError:
        pass
    finally:
        await http.close()
