"""Watching running apps: health checks, resource history, disk usage and alerts.

The supervisor calls `health_tick()` every second; the metrics collector calls
`record_tick()` every few seconds and, once a minute, this module writes one
sample per app (and one for the host) and evaluates the alert rules."""
import asyncio
import contextlib
import datetime
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from sqlmodel import Session, delete, select

from venvy import notify
from venvy.config import HTTP_CLIENT_LIMITS, DATA_DIR, _make_tz_aware, psutil, utc_now
from venvy.db import engine
from venvy.models import AlertRule, App, MetricSample

HISTORY_RETENTION_DAYS = int(os.getenv("VENVY_METRICS_RETENTION_DAYS", "7"))
_FLUSH_SEC = float(os.getenv("VENVY_METRICS_FLUSH_SEC", "60"))   # tests shorten it
_DISK_REFRESH_SEC = 600.0
_START_GRACE_SEC = 15.0
_PROBE_TIMEOUT_SEC = 5.0

# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------
# {app_id: {"status": "unknown" | "healthy" | "unhealthy", "failures": int,
#           "checked_at": iso, "code": int | None, "error": str | None,
#           "since": iso, "_next": monotonic, "_probing": bool, "_pid": int}}
_health: Dict[int, dict] = {}
# Apps that were reported unhealthy and have not been healthy since. Survives the
# restart a health check triggers, so the "healthy again" message still comes.
_reported_unhealthy: set = set()
_probe_tasks: set = set()      # held so a running probe is not garbage-collected
_health_restart_backoff: Dict[int, tuple] = {}   # app_id -> (count, not_before)
_probe_session: Optional[aiohttp.ClientSession] = None


def _get_probe_session() -> aiohttp.ClientSession:
    global _probe_session
    if _probe_session is None or _probe_session.closed:
        _probe_session = aiohttp.ClientSession(
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=_PROBE_TIMEOUT_SEC),
            **HTTP_CLIENT_LIMITS,
        )
    return _probe_session


async def close_probe_session() -> None:
    global _probe_session
    if _probe_session and not _probe_session.closed:
        await _probe_session.close()
    _probe_session = None


def health_info(app_id: int) -> Optional[dict]:
    """What the API shows for an app's health (None: it has no check)."""
    h = _health.get(app_id)
    if not h:
        return None
    return {k: v for k, v in h.items() if not k.startswith("_")}


def forget_health(app_id: int) -> None:
    _health.pop(app_id, None)
    _reported_unhealthy.discard(app_id)
    _health_restart_backoff.pop(app_id, None)


def forget_app(app_id: int) -> None:
    """Everything kept in memory about an app that was deleted. App ids are
    handed out again: the next app with this id must not inherit its disk size
    (and with it a disk alert) or its half-collected metrics."""
    forget_health(app_id)
    _disk_cache.pop(app_id, None)
    _acc.pop(app_id, None)
    _alert_since.pop(("disk", app_id), None)


def normalize_path(path: Optional[str]) -> Optional[str]:
    """A health-check path as stored: starts with /, no scheme or host."""
    if path is None:
        return None
    p = path.strip()
    if not p:
        return None
    if "://" in p or p.startswith("//") or any(c in p for c in " \t\r\n\0"):
        raise ValueError("The health check path must be a path on the app, like /health.")
    return p if p.startswith("/") else "/" + p


async def probe(port: int, path: str) -> dict:
    """One request to the app. Healthy is any answer below 400."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        http = _get_probe_session()
        async with http.get(url, allow_redirects=False) as resp:
            ok = resp.status < 400
            return {"ok": ok, "code": resp.status, "error": None if ok else f"HTTP {resp.status}"}
    except asyncio.TimeoutError:
        return {"ok": False, "code": None, "error": f"no answer within {int(_PROBE_TIMEOUT_SEC)} s"}
    except Exception as exc:
        return {"ok": False, "code": None, "error": str(exc)[:200] or exc.__class__.__name__}


_NOT_UP_YET = (502, 503, 504)


async def probe_up(port: int) -> dict:
    """Whether a web app that has no health check of its own answers on *port*
    the way a started one does (what a deploy waits for before it counts):

    - a connection refused, or closed before anything is said (docker-proxy
      in front of a container that isn't up yet), is not up;
    - an HTTP answer is up, unless it is 502, 503 or 504: a proxy in front of
      an app that isn't up yet (a compose project's nginx, say);
    - something that isn't HTTP but accepts the connection and waits for its
      own protocol, or answers in it (a database, a TCP service), is up."""
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), _PROBE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        return {"ok": False, "code": None, "error": f"no connection within {int(_PROBE_TIMEOUT_SEC)} s"}
    except OSError as exc:
        return {"ok": False, "code": None, "error": exc.strerror or str(exc)}
    try:
        try:
            first = await asyncio.wait_for(reader.read(1), 1.0)
            if not first:
                return {"ok": False, "code": None, "error": "the connection was closed at once"}
        except asyncio.TimeoutError:
            pass                 # holds the connection open, waiting for the client: fine so far
        except OSError as exc:
            return {"ok": False, "code": None, "error": exc.strerror or str(exc)}
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    try:
        http = _get_probe_session()
        async with http.get(f"http://127.0.0.1:{port}/", allow_redirects=False) as resp:
            ok = resp.status not in _NOT_UP_YET
            return {"ok": ok, "code": resp.status, "error": None if ok else f"HTTP {resp.status}",
                    "ms": (time.monotonic() - t0) * 1000}
    except Exception:
        # Accepted, but no HTTP: a service with a protocol of its own.
        return {"ok": True, "code": None, "error": None, "ms": (time.monotonic() - t0) * 1000}


def _stop_for_disk(app_id: int) -> None:
    """Stop an app (and its processes) that went over its disk limit with action "stop"."""
    async def go() -> None:
        from venvy import groups, process
        try:
            await groups.stop_children(app_id)
            with Session(engine) as s:
                await process.stop_app_process_logic(app_id, s, manual=True)
            logging.warning(f"App {app_id} stopped: over its disk limit.")
        except Exception as exc:
            logging.error(f"Could not stop app {app_id} over its disk limit: {exc}")
    from venvy.jobs import _running_tasks
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        t = loop.create_task(go())
        _running_tasks.add(t)
        t.add_done_callback(_running_tasks.discard)
    else:
        from venvy import notify as _n
        if _n._loop and not _n._loop.is_closed():
            def _start() -> None:
                t = asyncio.ensure_future(go())
                _running_tasks.add(t)
                t.add_done_callback(_running_tasks.discard)
            _n._loop.call_soon_threadsafe(_start)


def has_check(app) -> bool:
    """Whether the app has a health check that can run."""
    kind = getattr(app, "health_type", "http") or "http"
    if kind == "http":
        return bool(app.healthcheck_path and app.port)
    if kind == "tcp":
        return bool(app.port)
    if kind == "command":
        return bool(getattr(app, "health_command", None))
    return False


def describe_check(app) -> str:
    kind = getattr(app, "health_type", "http") or "http"
    if kind == "tcp":
        return f"TCP port {app.port}"
    if kind == "command":
        return "health command"
    return app.healthcheck_path or "/"


async def probe_tcp(port: int) -> dict:
    t0 = time.monotonic()
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), _PROBE_TIMEOUT_SEC)
        w.close()
        with contextlib.suppress(Exception):
            await w.wait_closed()
        return {"ok": True, "code": None, "error": None, "ms": (time.monotonic() - t0) * 1000}
    except asyncio.TimeoutError:
        return {"ok": False, "code": None, "error": f"no connection within {int(_PROBE_TIMEOUT_SEC)} s"}
    except OSError as exc:
        return {"ok": False, "code": None, "error": exc.strerror or str(exc)}


async def probe_command(app) -> dict:
    """Run the app's health command as its user, in its folder; exit 0 is healthy."""
    from venvy import appspec, groups
    from venvy.isolation import _as_app_user, _clean_root_env, _exec_env_argv, _isolation_active, _prepare_exec_env
    from venvy.process import _build_app_env
    spec = groups.effective(app)
    env = await asyncio.to_thread(_build_app_env, app, spec)
    argv = appspec.shell_argv(spec.health_command or app.health_command)
    tf = None
    try:
        if _isolation_active():
            tf, fd = _prepare_exec_env(env)
            argv, spawn_env, fds = _as_app_user(spec, _exec_env_argv(fd, argv)), _clean_root_env(), (fd,)
        else:
            spawn_env, fds = env, ()
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    cwd=str(appspec.work_dir(spec)), env=spawn_env, pass_fds=fds,
                                                    start_new_session=True)
    finally:
        if tf is not None:
            tf.close()
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=max(_PROBE_TIMEOUT_SEC, 30))
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        await proc.wait()
        return {"ok": False, "code": None, "error": "the health command timed out"}
    ok = proc.returncode == 0
    tail = out.decode(errors="replace").strip()[-200:]
    return {"ok": ok, "code": proc.returncode, "error": None if ok else (f"exit {proc.returncode}" + (f": {tail}" if tail else "")),
            "ms": (time.monotonic() - t0) * 1000}


async def run_check(app) -> dict:
    kind = getattr(app, "health_type", "http") or "http"
    if kind == "tcp":
        return await probe_tcp(app.port)
    if kind == "command":
        return await probe_command(app)
    t0 = time.monotonic()
    res = await probe(app.port, app.healthcheck_path)
    res["ms"] = (time.monotonic() - t0) * 1000
    return res


async def check_once(app) -> Optional[bool]:
    """One check now: True/False, or None when the app has no check."""
    if not has_check(app):
        return None
    try:
        return bool((await run_check(app))["ok"])
    except Exception:
        return False


_samples: List[tuple] = []


def _record_sample(app_id: int, ok: bool, ms: Optional[float]) -> None:
    _samples.append((f"app:{app_id}", ok, ms, utc_now()))


def take_samples() -> List[tuple]:
    global _samples
    out, _samples = _samples, []
    return out


async def _check_app(app_id: int, restart) -> None:
    h = _health[app_id]
    try:
        def _load():
            with Session(engine) as s:
                return s.get(App, app_id)
        app = await asyncio.to_thread(_load)
        if not app or app.status != "running" or not has_check(app):
            return
        res = await run_check(app)
        now = utc_now().isoformat()
        # The app may have been restarted while we waited.
        if _health.get(app_id) is not h or h.get("_pid") != app.pid:
            return
        _record_sample(app_id, bool(res["ok"]), res.get("ms"))
        h.update(checked_at=now, code=res["code"], error=res["error"], ms=res.get("ms"))
        if res["ok"]:
            was_bad = h["status"] == "unhealthy"
            h.update(status="healthy", failures=0)
            _health_restart_backoff.pop(app.id, None)
            if was_bad or h.get("since") is None:
                h["since"] = now
            if app.id in _reported_unhealthy:
                _reported_unhealthy.discard(app.id)
                notify.emit("app_recovered", f"{app.name} is healthy again", "", app.id, app.name)
            return
        h["failures"] += 1
        if h["failures"] < max(1, app.health_failures):
            return
        if h["status"] != "unhealthy":
            h.update(status="unhealthy", since=now)
            _reported_unhealthy.add(app.id)
            notify.emit("app_unhealthy", f"{app.name} is failing its health check",
                        f"{describe_check(app)}: {res['error']} ({h['failures']} checks in a row)",
                        app.id, app.name, "error")
            logging.warning(f"App '{app.name}' is unhealthy: {res['error']}")
        if app.health_restart:
            from venvy import deploy as _deploy
            if _deploy.is_running(app.id) or _deploy.is_running(app.parent_app_id or -1):
                return
            mono = time.monotonic()
            count, not_before = _health_restart_backoff.get(app.id, (0, 0.0))
            if mono < not_before:
                return
            delay = min(300.0, 10.0 * (2 ** count))
            _health_restart_backoff[app.id] = (count + 1, mono + delay)
            logging.warning(f"Restarting '{app.name}' after {h['failures']} failed health checks.")
            h["failures"] = 0
            await restart(app.id)
    except Exception as exc:
        logging.error(f"Health check of app {app_id} failed to run: {exc}")
    finally:
        h["_probing"] = False


async def health_tick(restart) -> None:
    """Start the probes that are due. `restart` is a coroutine function taking an app id."""
    def _running_apps():
        with Session(engine) as s:
            return s.exec(select(App).where(App.status == "running")).all()
    try:
        apps = await asyncio.to_thread(_running_apps)
    except Exception:
        return
    running = {a.id for a in apps}
    for app_id in [i for i in _health if i not in running]:
        _health.pop(app_id, None)
    mono = time.monotonic()
    for app in apps:
        if not has_check(app):
            _health.pop(app.id, None)
            continue
        h = _health.get(app.id)
        if h is None or h.get("_pid") != app.pid:
            # New process: start over, after a grace period to let it boot.
            h = _health[app.id] = {"status": "unknown", "failures": 0, "checked_at": None, "code": None,
                                   "error": None, "since": None, "_probing": False, "_pid": app.pid,
                                   "_next": mono + max(_START_GRACE_SEC, float(app.health_start_period or 0))}
        if h["_probing"] or mono < h["_next"]:
            continue
        h["_probing"] = True
        h["_next"] = mono + max(5, app.health_interval or 30)
        t = asyncio.ensure_future(_check_app(app.id, restart))
        _probe_tasks.add(t)
        t.add_done_callback(_probe_tasks.discard)


# ---------------------------------------------------------------------------
# Disk usage
# ---------------------------------------------------------------------------
_disk_cache: Dict[int, tuple] = {}      # app_id -> (monotonic, bytes)


_DIR_SIZE_MAX_DEPTH = 64
_DIR_SIZE_MAX_ENTRIES = 500_000


def dir_size(path: Path) -> int:
    """Bytes of the files under *path*, walking strictly by dir_fd with
    O_NOFOLLOW so an app swapping a folder for a symlink can never make root
    traverse or measure host directories."""
    import stat as _stat
    from venvy.safefs import _O_NOFOLLOW_DIR

    try:
        root_fd = os.open(path.resolve(), _O_NOFOLLOW_DIR)
    except OSError:
        return 0

    total = 0
    seen_entries = 0
    stack = [(root_fd, 0)]
    try:
        while stack:
            dfd, depth = stack.pop()
            try:
                with os.scandir(dfd) as it:
                    for entry in it:
                        seen_entries += 1
                        if seen_entries > _DIR_SIZE_MAX_ENTRIES:
                            return total
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if _stat.S_ISLNK(st.st_mode):
                            continue
                        if _stat.S_ISDIR(st.st_mode):
                            if depth < _DIR_SIZE_MAX_DEPTH:
                                try:
                                    sub_fd = os.open(entry.name, _O_NOFOLLOW_DIR, dir_fd=dfd)
                                    stack.append((sub_fd, depth + 1))
                                except OSError:
                                    continue
                        elif _stat.S_ISREG(st.st_mode):
                            total += st.st_size
            except OSError:
                pass
            finally:
                os.close(dfd)
    finally:
        for rem_fd, _ in stack:
            try:
                os.close(rem_fd)
            except OSError:
                pass
    return total


def cached_disk(app_id: int) -> Optional[int]:
    hit = _disk_cache.get(app_id)
    return hit[1] if hit else None


async def refresh_disk(app: App, force: bool = False) -> Optional[int]:
    hit = _disk_cache.get(app.id)
    if hit and not force and time.monotonic() - hit[0] < _DISK_REFRESH_SEC:
        return hit[1]
    size = await asyncio.to_thread(dir_size, Path(app.app_dir))
    _disk_cache[app.id] = (time.monotonic(), size)
    return size


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
_acc: Dict[int, List[float]] = {}        # app_id (0 = host) -> [cpu_sum, mem_sum, n]
_last_flush = time.monotonic()
_last_purge = 0.0
_alert_since: Dict[tuple, float] = {}    # virtual alerts (disk limits): key -> firing since


def record_tick(app_metrics: Dict[int, dict], host_cpu: float) -> None:
    """Called every metrics tick with the current numbers."""
    def add(key: int, cpu: float, mem: float) -> None:
        a = _acc.setdefault(key, [0.0, 0.0, 0])
        a[0] += cpu; a[1] += mem; a[2] += 1
    for app_id, m in app_metrics.items():
        add(app_id, float(m.get("cpu", 0.0)), float(m.get("memory", 0)))
    mem_pct = psutil.virtual_memory().percent if psutil else 0.0
    add(0, float(host_cpu), float(mem_pct))


async def flush_if_due() -> None:
    global _last_flush
    if time.monotonic() - _last_flush < _FLUSH_SEC:
        return
    _last_flush = time.monotonic()
    acc = dict(_acc)
    _acc.clear()
    try:
        await _flush(acc)
    except Exception as exc:
        logging.error(f"Writing metric history failed: {exc}")


async def _flush(acc: Dict[int, List[float]]) -> None:
    global _last_purge
    now = utc_now().replace(second=0, microsecond=0)
    host_disk = 0.0
    if psutil:
        with_path = DATA_DIR if DATA_DIR.exists() else Path("/")
        host_disk = await asyncio.to_thread(lambda: psutil.disk_usage(str(with_path)).percent)
    def _load_apps():
        with Session(engine) as s:
            return {a.id: a for a in s.exec(select(App)).all()}

    apps = await asyncio.to_thread(_load_apps)
    for app in apps.values():
        if app.id in acc or app.id not in _disk_cache:
            await refresh_disk(app)
    samples = []
    for key, (cpu_sum, mem_sum, n) in acc.items():
        if not n or (key != 0 and key not in apps):
            continue
        disk = host_disk if key == 0 else float(cached_disk(key) or 0)
        samples.append(MetricSample(app_id=key, ts=now, cpu=round(cpu_sum / n, 2), mem=mem_sum / n, disk=disk))
    # An app that is stopped still takes disk; record it so the chart is continuous.
    for app in apps.values():
        if app.id not in acc and cached_disk(app.id):
            samples.append(MetricSample(app_id=app.id, ts=now, cpu=0.0, mem=0.0, disk=float(cached_disk(app.id))))

    def _write_samples():
        with Session(engine) as s:
            for smp in samples:
                s.add(smp)
            s.commit()

    await asyncio.to_thread(_write_samples)
    await asyncio.to_thread(evaluate_alerts, {k: (v[0] / v[2], v[1] / v[2]) for k, v in acc.items() if v[2]}, host_disk, apps)
    if time.monotonic() - _last_purge > 3600:
        _last_purge = time.monotonic()
        cutoff = utc_now() - datetime.timedelta(days=HISTORY_RETENTION_DAYS)

        def _purge_old():
            with Session(engine) as s:
                s.exec(delete(MetricSample).where(MetricSample.ts < cutoff))
                s.commit()
            try:
                from venvy.audit import purge_audit
                purge_audit()
            except Exception as exc:
                logging.warning(f"Purging the audit trail failed: {exc}")

        await asyncio.to_thread(_purge_old)


def history(app_id: int, minutes: int, points: int = 240) -> List[dict]:
    """Samples of the last *minutes*, averaged into at most *points* buckets."""
    minutes = max(1, min(minutes, HISTORY_RETENTION_DAYS * 1440))
    points = max(1, min(points, 1000))
    since = utc_now() - datetime.timedelta(minutes=minutes)
    with Session(engine) as s:
        rows = s.exec(select(MetricSample).where(MetricSample.app_id == app_id, MetricSample.ts >= since)
                      .order_by(MetricSample.ts)).all()
    if not rows:
        return []
    step = max(1, -(-len(rows) // points))
    out = []
    for i in range(0, len(rows), step):
        chunk = rows[i:i + step]
        n = len(chunk)
        out.append({"ts": _make_tz_aware(chunk[-1].ts).isoformat(),
                    "cpu": round(sum(r.cpu for r in chunk) / n, 2),
                    "mem": sum(r.mem for r in chunk) / n,
                    "disk": chunk[-1].disk})
    return out


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------
RULE_METRICS = ("cpu", "memory", "disk")


def check_rule(app_id: int, metric: str, threshold: float, minutes: int) -> None:
    if metric not in RULE_METRICS:
        raise ValueError(f"metric must be one of: {', '.join(RULE_METRICS)}")
    if app_id == 0 and not (0 < threshold <= 100):
        raise ValueError("For the host the threshold is a percentage between 0 and 100.")
    if app_id != 0 and threshold <= 0:
        raise ValueError("The threshold must be above 0.")
    if not (1 <= minutes <= 1440):
        raise ValueError("minutes must be between 1 and 1440.")


def describe_rule(rule: AlertRule, app_name: Optional[str]) -> str:
    who = "The host" if rule.app_id == 0 else (app_name or f"App {rule.app_id}")
    unit = "%" if rule.app_id == 0 or rule.metric == "cpu" else " MB"
    what = {"cpu": "CPU", "memory": "memory", "disk": "disk"}[rule.metric]
    return f"{who}: {what} above {rule.threshold:g}{unit} for {rule.minutes} min"


def _value(rule: AlertRule, avg: Dict[int, tuple], host_disk: float, apps: Dict[int, App]) -> Optional[float]:
    if rule.app_id == 0:
        if rule.metric == "cpu":
            return avg.get(0, (None, None))[0]
        if rule.metric == "memory":
            return avg.get(0, (None, None))[1]
        return host_disk
    if rule.app_id not in apps:
        return None
    if rule.metric == "disk":
        d = cached_disk(rule.app_id)
        return d / 1048576 if d is not None else None
    cpu_mem = avg.get(rule.app_id)
    if cpu_mem is None:
        return None
    return cpu_mem[0] if rule.metric == "cpu" else cpu_mem[1] / 1048576


def evaluate_alerts(avg: Dict[int, tuple], host_disk: float, apps: Dict[int, App]) -> None:
    """Run once a minute with that minute's averages."""
    now = utc_now()
    with Session(engine) as s:
        for rule in s.exec(select(AlertRule).where(AlertRule.enabled == True)).all():  # noqa: E712
            val = _value(rule, avg, host_disk, apps)
            if val is None:
                # No reading (the app is stopped). A rule that was on its way to firing
                # starts over: kept, the old "since" fired it the moment the app was
                # back, however briefly it then went over.
                if rule.since is not None and not rule.firing:
                    rule.since = None
                    s.add(rule)
                continue
            app = apps.get(rule.app_id)
            label = describe_rule(rule, app.name if app else None)
            if val > rule.threshold:
                if rule.since is None:
                    rule.since = now
                held = (now - _make_tz_aware(rule.since)).total_seconds() / 60
                if not rule.firing and held >= rule.minutes - _FLUSH_SEC / 60 / 2:
                    rule.firing = True
                    notify.emit("alert_fired", label, f"Now at {val:.1f}.", rule.app_id or None,
                                app.name if app else None, "error")
            else:
                rule.since = None
                if rule.firing:
                    rule.firing = False
                    notify.emit("alert_cleared", label.replace(" above ", " back under "), f"Now at {val:.1f}.",
                                rule.app_id or None, app.name if app else None)
            s.add(rule)
        s.commit()
    # Each app's own soft disk limit.
    for app in apps.values():
        if not app.disk_limit_mb:
            if _alert_since.pop(("disk", app.id), None) is not None:
                notify.emit("alert_cleared", f"{app.name} is back under its disk limit", "The limit was removed.", app.id, app.name)
            continue
        d = cached_disk(app.id)
        if d is None:
            continue
        over = d / 1048576 > app.disk_limit_mb
        key = ("disk", app.id)
        if over and key not in _alert_since:
            _alert_since[key] = time.monotonic()
            stopping = getattr(app, "disk_limit_action", "alert") == "stop" and app.status == "running"
            notify.emit("alert_fired", f"{app.name} is over its disk limit",
                        f"{d / 1048576:.0f} MB used of {app.disk_limit_mb} MB."
                        + (" It is being stopped (disk limit action: stop)." if stopping else ""), app.id, app.name, "error")
            if stopping:
                _stop_for_disk(app.id)
        elif not over and key in _alert_since:
            _alert_since.pop(key)
            notify.emit("alert_cleared", f"{app.name} is back under its disk limit", "", app.id, app.name)
