"""Numbers for other tools, and checks of things beyond the apps.

* /metrics: Prometheus text format — per app: up, CPU, memory, restarts,
  health, requests through the proxy (by status class), bytes, latency
  histogram; per host: CPU, memory, disk; per URL monitor: up and latency.
  Read with an API token (scope read).
* OTLP: the same numbers pushed every minute to an OpenTelemetry collector
  (OTLP/HTTP JSON), when a collector URL is set.
* URL monitors: any URL (HTTP status) or TCP host:port, checked on a
  schedule, with notifications when one goes down or comes back.
* Uptime: every health-check and monitor result is kept for 90 days; the
  status page and the API show uptime percentages from them.
* Heartbeat: a URL (healthchecks.io, Better Stack, Uptime Kuma push …)
  Venvy calls every minute — when the host or Venvy itself dies, that
  service notices the silence and alerts. The one check no process on this
  host can do for itself.
* Status page: /status (when switched on), the chosen apps and monitors
  with their current state and 24 h / 30 d uptime, readable without login.
"""
import asyncio
import contextlib
import datetime
import html
import json
import re
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
from sqlalchemy import case
from sqlmodel import Session, delete, func, select

from venvy import kv, monitor, notify
from venvy.config import HTTP_CLIENT_LIMITS, psutil, utc_now
from venvy.db import engine
from venvy.models import App, UptimeSample, UrlMonitor

OTLP_KEY = "otlp"                 # {"url": ..., "headers": {...}} (secret)
HEARTBEAT_KEY = "heartbeat"       # {"url": ...} (secret)
STATUS_KEY = "status_page"        # {"enabled": bool, "title": str, "apps": [ids]}
RETENTION_DAYS = 90
_started = time.time()
_heartbeat_state = {"last_ok": None, "last_error": None}
_otlp_state = {"last_ok": None, "last_error": None}


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

def _esc(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def prometheus() -> str:
    from venvy import routing
    from venvy.process import _app_metrics, _restart_backoff
    out: List[str] = []

    def metric(name: str, kind: str, help_: str, rows: List[Tuple[Dict[str, str], float]]) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {kind}")
        for labels, value in rows:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
            out.append(f"{name}{{{lab}}} {value}" if lab else f"{name} {value}")

    with Session(engine) as s:
        apps = s.exec(select(App)).all()
        mons = s.exec(select(UrlMonitor)).all()

    def L(a: App) -> Dict[str, str]:
        return {"app": a.name, "app_id": str(a.id), "process": a.process_name or "web",
                "kind": a.process_kind or "app", "runtime": a.runtime}

    metric("venvy_app_up", "gauge", "1 when the process is running.",
           [(L(a), 1.0 if a.status == "running" else 0.0) for a in apps])
    metric("venvy_app_cpu_percent", "gauge", "CPU use (percent of one core).",
           [(L(a), float(_app_metrics.get(a.id, {}).get("cpu", 0.0))) for a in apps])
    metric("venvy_app_memory_bytes", "gauge", "Resident memory of the process tree.",
           [(L(a), float(_app_metrics.get(a.id, {}).get("memory", 0))) for a in apps])
    metric("venvy_app_restart_failures", "gauge", "Crashes in a row (restart back-off).",
           [(L(a), float(_restart_backoff.get(a.id, (0, 0))[0])) for a in apps])
    health = []
    for a in apps:
        h = monitor.health_info(a.id)
        if h:
            health.append((L(a), 1.0 if h.get("status") == "healthy" else 0.0 if h.get("status") == "unhealthy" else -1.0))
    metric("venvy_app_healthy", "gauge", "1 healthy, 0 failing its health check, -1 not checked yet.", health)
    disk = [(L(a), float(monitor.cached_disk(a.id) or 0)) for a in apps if not a.parent_app_id]
    metric("venvy_app_disk_bytes", "gauge", "Size of the app's folder.", disk)

    names = {a.id: a.name for a in apps}
    req_rows, byte_rows, hist = [], [], []
    for app_id, st in list(routing.stats.items()):
        base = {"app": names.get(app_id, str(app_id)), "app_id": str(app_id)}
        for cls, n in st["requests"].items():
            req_rows.append(({**base, "code": cls}, float(n)))
        byte_rows.append((base, float(st["bytes"])))
        cum = 0
        for i, b in enumerate(routing.BUCKETS):
            cum += st["buckets"][i]
            hist.append(("venvy_http_request_duration_seconds_bucket", {**base, "le": str(b)}, float(cum)))
        cum += st["buckets"][-1]
        hist.append(("venvy_http_request_duration_seconds_bucket", {**base, "le": "+Inf"}, float(cum)))
        hist.append(("venvy_http_request_duration_seconds_sum", base, float(st["sum"])))
        hist.append(("venvy_http_request_duration_seconds_count", base, float(cum)))
    metric("venvy_http_requests_total", "counter", "Requests through Venvy's proxy.", req_rows)
    metric("venvy_http_response_bytes_total", "counter", "Response bytes sent through Venvy's proxy.", byte_rows)
    out.append("# HELP venvy_http_request_duration_seconds Time to answer through the proxy.")
    out.append("# TYPE venvy_http_request_duration_seconds histogram")
    for name, labels, v in hist:
        lab = ",".join(f'{k}="{_esc(x)}"' for k, x in labels.items())
        out.append(f"{name}{{{lab}}} {v}")

    metric("venvy_monitor_up", "gauge", "1 when the URL monitor's last check passed.",
           [({"monitor": m.name, "target": m.target}, 1.0 if m.last_ok else 0.0) for m in mons if m.last_ok is not None])
    metric("venvy_monitor_latency_ms", "gauge", "Latency of the last check.",
           [({"monitor": m.name, "target": m.target}, float(m.last_ms or 0)) for m in mons if m.last_ms is not None])

    if psutil:
        with contextlib.suppress(Exception):
            vm = psutil.virtual_memory()
            du = psutil.disk_usage("/")
            from venvy.process import host_cpu_percent
            metric("venvy_host_cpu_percent", "gauge", "Host CPU use.", [({}, float(host_cpu_percent()))])
            metric("venvy_host_memory_percent", "gauge", "Host memory use.", [({}, float(vm.percent))])
            metric("venvy_host_disk_percent", "gauge", "Use of the root filesystem.", [({}, float(du.percent))])
    metric("venvy_uptime_seconds", "gauge", "How long this Venvy has been running.", [({}, time.time() - _started)])
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# OTLP push
# ---------------------------------------------------------------------------

def _otlp_payload() -> dict:
    """The /metrics numbers as OTLP gauges."""
    now = str(int(time.time() * 1e9))
    metrics: Dict[str, list] = {}
    for line in prometheus().splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (\S+)$", line)
        if not m:
            continue
        name, labels, value = m.group(1), m.group(3) or "", m.group(4)
        attrs = [{"key": k, "value": {"stringValue": v.replace('\\"', '"')}}
                 for k, v in re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', labels)]
        with contextlib.suppress(ValueError):
            metrics.setdefault(name, []).append({"asDouble": float(value), "timeUnixNano": now, "attributes": attrs})
    import socket
    return {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "venvy"}},
                                    {"key": "host.name", "value": {"stringValue": socket.gethostname()}}]},
        "scopeMetrics": [{"scope": {"name": "venvy"},
                          "metrics": [{"name": n, "gauge": {"dataPoints": pts}} for n, pts in metrics.items()]}],
    }]}


async def push_otlp(http: aiohttp.ClientSession) -> None:
    cfg = kv.get_json(OTLP_KEY, None, secret=True)
    if not cfg or not cfg.get("url"):
        return
    url = cfg["url"].rstrip("/")
    if not url.endswith("/v1/metrics"):
        url += "/v1/metrics"
    headers = {"Content-Type": "application/json", **{str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}}
    try:
        async with http.post(url, data=json.dumps(_otlp_payload()), headers=headers) as r:
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status}")
        _otlp_state.update(last_ok=utc_now().isoformat(), last_error=None)
    except Exception as exc:
        _otlp_state.update(last_error=str(exc)[:200] or exc.__class__.__name__)


# ---------------------------------------------------------------------------
# URL monitors
# ---------------------------------------------------------------------------

_TARGET_RE = re.compile(r"(https?://\S+|tcp://[A-Za-z0-9.\-\[\]:]+:\d{1,5})")


def target_host(t: str) -> str:
    import urllib.parse
    if t.startswith("tcp://"):
        return t[6:].rpartition(":")[0].strip("[]").lower()
    return (urllib.parse.urlsplit(t).hostname or "").lower()


def clean_target(t: str, local_ok: bool = True) -> str:
    """A monitor target. Never a cloud-metadata / link-local address; with
    *local_ok* False (not an admin) not this host either — the check runs as
    Venvy, past the firewall that keeps apps off each other's ports."""
    import ipaddress
    from venvy.notify import _check_safe_outbound_host
    t = (t or "").strip()
    if not _TARGET_RE.fullmatch(t) or len(t) > 2000:
        raise ValueError("The target must be an http(s):// URL or tcp://host:port.")
    host = target_host(t)
    _check_safe_outbound_host(host, "target")
    if not local_ok:
        local = host in ("localhost", "localhost.localdomain") or host.endswith(".localhost")
        try:
            ip = ipaddress.ip_address(host)
            ip = ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip
            local = local or ip.is_loopback or ip.is_unspecified
        except ValueError:
            pass
        if local:
            raise ValueError("Only an admin can monitor addresses on this host.")
    return t


async def check_target(http: aiohttp.ClientSession, m: UrlMonitor) -> Tuple[bool, Optional[float], str]:
    t0 = time.monotonic()
    try:
        if m.target.startswith("tcp://"):
            hostport = m.target[6:]
            host, _, port = hostport.rpartition(":")
            _r, w = await asyncio.wait_for(asyncio.open_connection(host.strip("[]"), int(port)), m.timeout)
            w.close()
            return True, (time.monotonic() - t0) * 1000, ""
        async with http.get(m.target, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=m.timeout)) as r:
            ok = (r.status == m.expect_status) if m.expect_status else r.status < 400
            return ok, (time.monotonic() - t0) * 1000, "" if ok else f"HTTP {r.status}"
    except asyncio.TimeoutError:
        return False, None, f"no answer within {m.timeout} s"
    except Exception as exc:
        return False, None, str(exc)[:200] or exc.__class__.__name__


_mon_next: Dict[int, float] = {}


async def run_monitors(http: aiohttp.ClientSession) -> None:
    with Session(engine) as s:
        mons = [m for m in s.exec(select(UrlMonitor)).all() if m.enabled]
    now = time.monotonic()
    for m in mons:
        if now < _mon_next.get(m.id, 0):
            continue
        _mon_next[m.id] = now + max(10, m.interval)
        ok, ms, err = await check_target(http, m)
        with Session(engine) as s:
            row = s.get(UrlMonitor, m.id)
            if row is None:
                continue
            before = row.last_ok
            row.last_ok, row.last_ms, row.last_error, row.last_checked = ok, ms, err, utc_now()
            s.add(row)
            s.add(UptimeSample(target=f"mon:{m.id}", ok=ok, ms=ms))
            s.commit()
        if before is not None and before != ok:
            if ok:
                notify.emit("monitor_up", f"{m.name} is up again", m.target)
            else:
                notify.emit("monitor_down", f"{m.name} is down", f"{m.target}: {err}", level="error")


# ---------------------------------------------------------------------------
# Uptime
# ---------------------------------------------------------------------------

def store_health_samples() -> None:
    rows = monitor.take_samples()
    if not rows:
        return
    with Session(engine) as s:
        for target, ok, ms, ts in rows:
            s.add(UptimeSample(target=target, ok=ok, ms=ms, ts=ts))
        s.commit()


def purge_samples() -> None:
    cutoff = utc_now() - datetime.timedelta(days=RETENTION_DAYS)
    with Session(engine) as s:
        s.exec(delete(UptimeSample).where(UptimeSample.ts < cutoff))
        s.commit()


def uptime(target: str, hours: int) -> Optional[float]:
    since = utc_now() - datetime.timedelta(hours=hours)
    with Session(engine) as s:
        # One pass over the samples: how many, and how many of them good.
        total, good = s.exec(select(func.count(), func.sum(case((UptimeSample.ok == True, 1), else_=0)))  # noqa: E712
                             .where(UptimeSample.target == target, UptimeSample.ts >= since)).one()
    if not total:
        return None
    return round(100.0 * (good or 0) / total, 3)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

async def heartbeat(http: aiohttp.ClientSession) -> None:
    cfg = kv.get_json(HEARTBEAT_KEY, None, secret=True)
    if not cfg or not cfg.get("url"):
        return
    try:
        async with http.get(cfg["url"], timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status >= 400:
                raise RuntimeError(f"HTTP {r.status}")
        _heartbeat_state.update(last_ok=utc_now().isoformat(), last_error=None)
    except Exception as exc:
        _heartbeat_state.update(last_error=str(exc)[:200] or exc.__class__.__name__)


def states() -> dict:
    return {"heartbeat": dict(_heartbeat_state), "otlp": dict(_otlp_state)}


# ---------------------------------------------------------------------------
# Status page
# ---------------------------------------------------------------------------

# The status page is public: anyone can ask for it as often as they like, and
# each answer counts a month of samples per item. One answer serves everyone
# for a few seconds (the page itself reloads once a minute).
_STATUS_CACHE_SEC = 10.0
_status_cache: Dict[str, object] = {"at": 0.0, "cfg": None, "data": None}


def status_data() -> Optional[dict]:
    cfg = kv.get_json(STATUS_KEY, {}) or {}
    if not cfg.get("enabled"):
        return None
    now = time.monotonic()
    if _status_cache["cfg"] == cfg and now - float(_status_cache["at"]) < _STATUS_CACHE_SEC:
        return _status_cache["data"]          # type: ignore[return-value]
    data = _status_data(cfg)
    _status_cache.update(at=now, cfg=cfg, data=data)
    return data


def _status_data(cfg: dict) -> dict:
    want = {int(x) for x in cfg.get("apps", [])}
    items = []
    with Session(engine) as s:
        for a in s.exec(select(App).where(App.parent_app_id == None)).all():  # noqa: E711
            if a.id not in want:
                continue
            h = monitor.health_info(a.id) or {}
            up = a.status == "running" and h.get("status") != "unhealthy"
            items.append({"name": a.name, "up": up, "state": "operational" if up else ("degraded" if a.status == "running" else "down"),
                          "uptime_24h": uptime(f"app:{a.id}", 24), "uptime_30d": uptime(f"app:{a.id}", 24 * 30)})
        for m in s.exec(select(UrlMonitor).where(UrlMonitor.public == True)).all():  # noqa: E712
            items.append({"name": m.name, "up": bool(m.last_ok), "state": "operational" if m.last_ok else "down",
                          "uptime_24h": uptime(f"mon:{m.id}", 24), "uptime_30d": uptime(f"mon:{m.id}", 24 * 30)})
    return {"title": cfg.get("title") or "Status", "items": items, "generated": utc_now().isoformat(),
            "all_up": all(i["up"] for i in items)}


def status_html(data: dict) -> str:
    rows = []
    for i in data["items"]:
        color = "#16a34a" if i["up"] else "#dc2626"
        def pct(v):
            return "–" if v is None else f"{v:.2f}%"
        rows.append(f"<tr><td><span style='display:inline-block;width:.7em;height:.7em;border-radius:50%;"
                    f"background:{color};margin-right:.5em'></span>{html.escape(i['name'])}</td>"
                    f"<td>{html.escape(i['state'])}</td><td>{pct(i['uptime_24h'])}</td><td>{pct(i['uptime_30d'])}</td></tr>")
    head = "All systems operational" if data["all_up"] else "Some systems are having problems"
    title = html.escape(data["title"])
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta http-equiv=refresh content=60><title>{title}</title>"
            "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:46rem;margin:3rem auto;padding:0 1rem;color:#222}"
            "table{width:100%;border-collapse:collapse}td,th{padding:.6rem .4rem;border-bottom:1px solid #eee;text-align:left}"
            "th{font-weight:600;color:#666;font-size:.85em}.b{padding:1rem;border-radius:.5rem;margin:1.5rem 0;color:#fff}"
            "@media(prefers-color-scheme:dark){body{background:#111;color:#ddd}td,th{border-color:#333}}</style>"
            f"<h1>{title}</h1><div class=b style='background:{'#16a34a' if data['all_up'] else '#dc2626'}'>{head}</div>"
            "<table><tr><th>Service</th><th>State</th><th>24 h</th><th>30 days</th></tr>"
            + "".join(rows) + f"</table><p style='color:#888;font-size:.85em'>Updated {html.escape(data['generated'][:19])} UTC</p></html>")


# ---------------------------------------------------------------------------
# Containers: what docker apps use (their CLI process is not where the work is)
# ---------------------------------------------------------------------------

def _parse_size(v: str) -> float:
    v = v.strip()
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", v)
    if not m:
        return 0.0
    mult = {"B": 1, "KB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1024 ** 2, "GB": 1e9, "GiB": 1024 ** 3,
            "TB": 1e12, "TiB": 1024 ** 4}.get(m.group(2), 1)
    return float(m.group(1)) * mult


async def container_metrics() -> None:
    import shutil as _sh
    from venvy.process import container_usage
    if not _sh.which("docker"):
        return
    with Session(engine) as s:
        apps = [a for a in s.exec(select(App).where(App.runtime.in_(("docker", "compose")))).all() if a.status == "running"]
    if not apps:
        return
    proc = await asyncio.create_subprocess_exec(
        "docker", "stats", "--no-stream", "--format", "{{json .}}",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin"})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return
    usage: Dict[int, List[float]] = {}
    for line in out.decode(errors="replace").splitlines():
        with contextlib.suppress(ValueError, KeyError):
            row = json.loads(line)
            name = row["Name"]
            m = re.match(r"venvy-app-(\d+)(?:[-_].*)?$", name)
            if not m:
                continue
            cpu = float(row.get("CPUPerc", "0").rstrip("%") or 0)
            mem = _parse_size(row.get("MemUsage", "0B").split("/")[0])
            acc = usage.setdefault(int(m.group(1)), [0.0, 0.0])
            acc[0] += cpu
            acc[1] += mem
    container_usage.clear()
    for a in apps:
        if a.id in usage:
            container_usage[a.id] = {"cpu": round(usage[a.id][0], 1), "memory": int(usage[a.id][1])}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

async def telemetry_task() -> None:
    http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **HTTP_CLIENT_LIMITS)
    last_minute = 0.0
    last_purge = 0.0
    last_containers = 0.0
    try:
        while True:
            await asyncio.sleep(5)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(store_health_samples)
            with contextlib.suppress(Exception):
                await run_monitors(http)
            now = time.monotonic()
            if now - last_containers >= 15:
                last_containers = now
                with contextlib.suppress(Exception):
                    await container_metrics()
            if now - last_minute >= 60:
                last_minute = now
                with contextlib.suppress(Exception):
                    await heartbeat(http)
                with contextlib.suppress(Exception):
                    await push_otlp(http)
            if now - last_purge >= 3600:
                last_purge = now
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(purge_samples)
    except asyncio.CancelledError:
        pass
    finally:
        await http.close()
