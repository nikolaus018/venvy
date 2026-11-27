"""What Venvy's proxy does with a request for a hostname that is not Venvy's own.

Rules come from three places:

* an app's ``domain`` (the simple case: that hostname, every path),
* ``Route`` rows: more hostnames per app (aliases), wildcards
  (``*.example.com``), path routes (``example.com/api`` → another app,
  optionally with the prefix stripped), redirects (``www.example.com`` →
  ``example.com``), each with its own controls,
* the hostnames an app is published under through the Cloudflare tunnel,
  so a tunnel route can point at Venvy and still reach the app.

The most specific rule wins: an exact hostname before a wildcard, then the
longest path prefix. The upstreams of an app are its own port and those of
its running replicas, taken in turn (round robin), skipping ones whose health
check is failing while others are fine.

Per-rule controls: IP allow-list, HTTP basic auth (scrypt hashes), rate
limit per client IP, request body limit, extra response headers, gzip, and a
small cache for public GET responses. Requests are counted per app (for
/metrics) and written to its access log.
"""
import asyncio
import base64
import collections
import contextlib
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from venvy import own_hosts
from venvy.config import DATA_DIR
from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.models import App, CfRoute, Route

ACCESS_LOG_DIR = DATA_DIR / "access-logs"
_TTL = 5.0


@dataclasses.dataclass
class Rule:
    host: str
    prefix: str
    app_id: int
    kind: str = "proxy"
    strip: bool = False
    redirect_to: Optional[str] = None
    code: int = 301
    ip_allow: Tuple = ()
    users: Dict[str, str] = dataclasses.field(default_factory=dict)
    rate_limit: int = 0
    max_body: int = 0
    headers: Tuple = ()
    gzip: bool = False
    cache_seconds: int = 0
    route_id: Optional[int] = None


@dataclasses.dataclass
class Target:
    app_id: int
    name: str
    ports: List[Tuple[int, int]]        # (process app id, port) of running processes
    access_log: bool = True


_rules: Dict[str, List[Rule]] = {}
_wild: List[Tuple[str, List[Rule]]] = []
_targets: Dict[int, Target] = {}
_built_at = 0.0
_lock: Optional[asyncio.Lock] = None
_rr: Dict[int, int] = collections.defaultdict(int)


_gen = 0          # bumped by every invalidate(); a build that saw an older one doesn't count


def invalidate() -> None:
    global _built_at, _gen
    _gen += 1
    _built_at = 0.0


def age() -> float:
    return time.monotonic() - _built_at


def _networks(raw: str) -> Tuple:
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            with contextlib.suppress(ValueError):
                out.append(ipaddress.ip_network(part, strict=False))
    return tuple(out)


def _users(raw: str) -> Dict[str, str]:
    if not raw:
        return {}
    ok, plain = _try_decrypt_secret(raw)
    if not ok:
        return {"\0locked": "x"}         # undecryptable: nobody gets in
    with contextlib.suppress(ValueError):
        d = json.loads(plain or "{}")
        if isinstance(d, dict):
            return {str(k): str(v) for k, v in d.items()}
    return {}


def hostname_apps(session: Session, host: str, skip_route: Optional[int] = None) -> set:
    """The ids of the apps *host* already leads to: as their domain, through a
    route (other than *skip_route*) or through the Cloudflare tunnel."""
    host = (host or "").lower()
    ids = {a.id for a in session.exec(select(App).where(App.domain == host)).all()}
    ids |= {r.app_id for r in session.exec(select(Route).where(Route.hostname == host)).all() if r.id != skip_route}
    ids |= {c.app_id for c in session.exec(select(CfRoute).where(CfRoute.hostname == host)).all() if c.app_id}
    return ids


def _build() -> None:
    global _rules, _wild, _targets, _built_at
    from venvy import monitor
    started_gen = _gen
    rules: Dict[str, List[Rule]] = collections.defaultdict(list)
    targets: Dict[int, Target] = {}
    with Session(engine) as s:
        apps = s.exec(select(App)).all()
        routes = s.exec(select(Route)).all()
        cf = s.exec(select(CfRoute)).all()
    by_id = {a.id: a for a in apps}
    for a in apps:
        if a.parent_app_id:
            continue
        procs = [a] + [c for c in apps if c.parent_app_id == a.id and c.process_kind == "replica"]
        live = [(p.id, p.port) for p in procs if p.status == "running" and p.port]
        healthy = [x for x in live if (monitor.health_info(x[0]) or {}).get("status") != "unhealthy"]
        targets[a.id] = Target(a.id, a.name, healthy or live, bool(a.access_log))
        if a.domain:
            rules[a.domain.lower()].append(Rule(host=a.domain.lower(), prefix="/", app_id=a.id))
    for r in routes:
        if r.app_id not in by_id:
            continue
        try:
            headers = tuple((str(k), str(v)) for k, v in (json.loads(r.headers or "{}") or {}).items())
        except ValueError:
            headers = ()
        rules[r.hostname.lower()].append(Rule(
            host=r.hostname.lower(), prefix=r.path_prefix or "/", app_id=r.app_id, kind=r.kind, strip=r.strip_prefix,
            redirect_to=r.redirect_to, code=r.redirect_code or 301, ip_allow=_networks(r.ip_allow),
            users=_users(r.basic_auth), rate_limit=r.rate_limit or 0, max_body=(r.max_body_mb or 0) * 1024 * 1024,
            headers=headers, gzip=r.gzip, cache_seconds=r.cache_seconds or 0, route_id=r.id))
    for c in cf:
        h = (c.hostname or "").lower()
        if h and c.app_id in by_id and h not in rules:
            rules[h].append(Rule(host=h, prefix="/", app_id=c.app_id))
    for h in rules:
        rules[h].sort(key=lambda x: len(x.prefix), reverse=True)
    wild = sorted(((h[2:], v) for h, v in rules.items() if h.startswith("*.")), key=lambda x: len(x[0]), reverse=True)
    _rules = {h: v for h, v in rules.items() if not h.startswith("*.")}
    _wild = wild
    _targets = targets
    # Something changed while the database was read: this table may already be
    # out of date, so the next request builds it again.
    _built_at = time.monotonic() if started_gen == _gen else 0.0


async def _ensure() -> None:
    global _lock
    if time.monotonic() - _built_at <= _TTL:
        return
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if time.monotonic() - _built_at <= _TTL:
            return
        try:
            await asyncio.to_thread(_build)
        except Exception as exc:
            logging.warning("Could not read the routes: %s", exc)


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_AMBIGUOUS_RE = re.compile(r"%2[Ff]|%5[Cc]|\\")


def normalize_path(raw: str) -> str:
    """The path rules are matched against: percent-escapes of unreserved
    characters decoded (others upper-cased), '.' and '..' segments resolved,
    repeated slashes merged — so '/%61dmin', '//admin' or '/x/../admin' can't
    miss an '/admin' route that the app would then serve anyway."""
    def dec(m) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()
    p = _PCT_RE.sub(dec, (raw or "/").split("?", 1)[0])
    segs: List[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    out = "/" + "/".join(segs)
    if out != "/" and (p.endswith("/") or p.endswith("/.") or p.endswith("/..")):
        out += "/"
    return out


def ambiguous_path(raw: str) -> bool:
    """An encoded slash or a backslash, which an app may turn into a path
    separator after Venvy has picked the route."""
    return bool(_AMBIGUOUS_RE.search((raw or "").split("?", 1)[0]))


def path_sensitive(host: str, rule: "Rule") -> bool:
    """Whether the path decides where requests for *host* go (path routes):
    then the app must be sent the path the route was chosen with."""
    if rule.prefix not in ("", "/"):
        return True
    name = own_hosts.host_name(host)
    lst = _rules.get(name)
    if lst is None:
        lst = next((v for s, v in _wild if name.endswith("." + s) and name != s), [])
    return len(lst) > 1


def _prefix_match(path: str, prefix: str) -> bool:
    if prefix in ("", "/"):
        return True
    p = prefix.rstrip("/")
    return path == p or path.startswith(p + "/") or path.startswith(p + "?")


def _match_list(rules: List[Rule], path: str) -> Optional[Rule]:
    for r in rules:
        if _prefix_match(path, r.prefix):
            return r
    return None


def match_sync(host: str, path: str) -> Optional[Rule]:
    name = own_hosts.host_name(host)
    if not name or own_hosts.is_own(name):
        return None
    lst = _rules.get(name)
    if lst:
        r = _match_list(lst, path)
        if r:
            return r
    for suffix, lst in _wild:
        if name.endswith("." + suffix) and name != suffix:
            r = _match_list(lst, path)
            if r:
                return r
    return None


async def match(host: str, path: str) -> Optional[Rule]:
    await _ensure()
    r = match_sync(host, path)
    if r is None and _built_at and time.monotonic() - _built_at > 0.5:
        # A hostname added a moment ago: look once more before saying no.
        invalidate()
        await _ensure()
        r = match_sync(host, path)
    return r


def is_routed(host: str) -> bool:
    """Whether *host* has any rule (used by Venvy's own hostname checks)."""
    name = own_hosts.host_name(host)
    return bool(name and (name in _rules or any(name.endswith("." + s) for s, _ in _wild)))


# Processes being taken out of rotation (a rolling restart): no new requests
# go to them while others can take them; what they are serving finishes.
_draining: set = set()
inflight: Dict[int, int] = collections.defaultdict(int)       # process app id -> requests in progress


def drain(process_id: int) -> None:
    _draining.add(process_id)


def undrain(process_id: int) -> None:
    _draining.discard(process_id)


async def wait_idle(process_id: int, timeout: float = 30.0) -> None:
    end = time.monotonic() + timeout
    while inflight.get(process_id, 0) > 0 and time.monotonic() < end:
        await asyncio.sleep(0.1)


def upstreams(app_id: int) -> List[Tuple[int, int]]:
    """The processes to try for an app, the next one in turn first."""
    t = _targets.get(app_id)
    if not t or not t.ports:
        return []
    ports = [x for x in t.ports if x[0] not in _draining] or t.ports
    n = len(ports)
    i = _rr[app_id] % n
    _rr[app_id] = (i + 1) % n
    return ports[i:] + ports[:i]


def target(app_id: int) -> Optional[Target]:
    return _targets.get(app_id)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def _check_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, h_b64 = stored.split("$")
        salt, want = base64.b64decode(salt_b64), base64.b64decode(h_b64)
    except ValueError:
        return False
    got = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(got, want)


# {(route id, Authorization header): (expires, the stored hash it was checked against)}
_auth_ok: Dict[Tuple[int, str], Tuple[float, str]] = {}


# Wrong passwords per (route, client address) in the last minute. Past the
# limit every attempt is refused without being checked: otherwise anyone could
# guess a route's password as fast as the server runs scrypt — and keep its
# worker threads busy doing it.
_AUTH_FAIL_MAX = 10
_AUTH_FAIL_WINDOW = 60.0
_auth_fail: Dict[Tuple[int, str], collections.deque] = {}


# What an unknown username is checked against, so that it takes as long as a
# wrong password and the answer time doesn't tell which usernames exist.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _auth_failures(key: Tuple[int, str], now: float) -> collections.deque:
    dq = _auth_fail.get(key)
    if dq is None:
        if len(_auth_fail) > 50000:
            for k in [k for k, v in _auth_fail.items() if not v or now - v[-1] > _AUTH_FAIL_WINDOW]:
                _auth_fail.pop(k, None)
            if len(_auth_fail) > 50000:
                _auth_fail.clear()
        dq = _auth_fail[key] = collections.deque()
    while dq and now - dq[0] > _AUTH_FAIL_WINDOW:
        dq.popleft()
    return dq


async def basic_auth_ok(rule: Rule, header: Optional[str], ip: str = "") -> bool:
    if not rule.users:
        return True
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")
    except Exception:
        return False
    stored = rule.users.get(user)
    key = (rule.route_id or 0, header)
    now = time.monotonic()
    hit = _auth_ok.get(key)
    # Only while the password it was checked against is still the one stored.
    if hit is not None and hit[0] > now and stored is not None and hmac.compare_digest(hit[1], stored):
        return True
    fails = _auth_failures((rule.route_id or 0, ip), now)
    if len(fails) >= _AUTH_FAIL_MAX:
        return False
    ok = await asyncio.to_thread(_check_password, pw, stored if stored is not None else _DUMMY_HASH)
    ok = ok and stored is not None
    if ok:
        if len(_auth_ok) > 10000:
            _auth_ok.clear()
        _auth_ok[key] = (now + 300, stored)     # skip scrypt for this exact header for 5 minutes
    else:
        fails.append(now)
    return ok


def encode_users(users: Dict[str, str]) -> str:
    """{user: password or existing scrypt hash} -> stored form."""
    out = {}
    for u, pw in users.items():
        out[u] = pw if pw.startswith("scrypt$") else hash_password(pw)
    return _encrypt_secret(json.dumps(out)) if out else ""


def ip_allowed(rule: Rule, ip: str) -> bool:
    if not rule.ip_allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return any(addr in net for net in rule.ip_allow)


_hits: Dict[Tuple[int, str], collections.deque] = {}


def rate_ok(rule: Rule, ip: str) -> bool:
    if not rule.rate_limit:
        return True
    key = (rule.route_id or 0, ip)
    now = time.monotonic()
    dq = _hits.get(key)
    if dq is None:
        if len(_hits) > 50000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 60][:25000]:
                _hits.pop(k, None)
        dq = _hits[key] = collections.deque()
    while dq and now - dq[0] > 60:
        dq.popleft()
    if len(dq) >= rule.rate_limit:
        return False
    dq.append(now)
    return True


# --- cache -------------------------------------------------------------------

_CACHE_MAX_ENTRIES = 2000
_CACHE_MAX_BODY = 2 * 1024 * 1024
_cache: "collections.OrderedDict[tuple, tuple]" = collections.OrderedDict()


def cache_get(key: tuple):
    hit = _cache.get(key)
    if not hit:
        return None
    if hit[0] < time.monotonic():
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return hit[1], hit[2], hit[3]


def cache_put(key: tuple, ttl: float, status: int, headers: list, body: bytes) -> None:
    if ttl <= 0 or len(body) > _CACHE_MAX_BODY:
        return
    _cache[key] = (time.monotonic() + ttl, status, headers, body)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        _cache.popitem(last=False)


def cache_ttl(rule: Rule, resp_headers: Dict[str, str]) -> float:
    """How long a response may be kept: only public responses without cookies."""
    if not rule.cache_seconds:
        return 0
    if "set-cookie" in resp_headers:
        return 0
    # An answer that depends on more than the encoding (Vary: Cookie, a
    # language, "*") is not the same for the next client: the key doesn't hold it.
    vary = {v.strip().lower() for v in resp_headers.get("vary", "").split(",") if v.strip()}
    if vary - {"accept-encoding"}:
        return 0
    cc = resp_headers.get("cache-control", "").lower()
    if any(x in cc for x in ("private", "no-store", "no-cache")):
        return 0
    ttl = float(rule.cache_seconds)
    for part in cc.split(","):
        part = part.strip()
        if part.startswith("s-maxage=") or part.startswith("max-age="):
            with contextlib.suppress(ValueError):
                ttl = min(ttl, float(part.split("=", 1)[1]))
    if "max-age" not in cc and "public" not in cc and "expires" not in resp_headers:
        return 0
    return ttl


def invalidate_cache(app_id: Optional[int] = None) -> None:
    if app_id is None:
        _cache.clear()
        return
    for k in [k for k in _cache if k[0] == app_id]:
        _cache.pop(k, None)


# --- metrics and access logs --------------------------------------------------

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
stats: Dict[int, dict] = collections.defaultdict(lambda: {"requests": collections.Counter(), "bytes": 0,
                                                          "buckets": [0] * (len(BUCKETS) + 1), "sum": 0.0})
_log_buf: Dict[int, List[str]] = collections.defaultdict(list)
_ACCESS_MAX = 20 * 1024 * 1024


def record(app_id: int, method: str, path: str, status: int, size: int, seconds: float, ip: str, host: str,
           ua: str, log_it: bool = True) -> None:
    st = stats[app_id]
    st["requests"][f"{status // 100}xx"] += 1
    st["bytes"] += size
    st["sum"] += seconds
    for i, b in enumerate(BUCKETS):
        if seconds <= b:
            st["buckets"][i] += 1
            break
    else:
        st["buckets"][-1] += 1
    if log_it:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ua = ua.replace('"', "'")[:300]
        line = f'{ts} {ip} "{method} {path[:2000]}" {status} {size} {seconds * 1000:.1f}ms {host} "{ua}"'
        _log_buf[app_id].append(line.replace("\n", " ").replace("\r", " "))


def access_log_file(app_id: int) -> Path:
    return ACCESS_LOG_DIR / f"{app_id}.log"


def _flush_logs() -> None:
    if not _log_buf:
        return
    ACCESS_LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    for app_id in list(_log_buf):
        lines = _log_buf.pop(app_id, [])
        if not lines:
            continue
        f = access_log_file(app_id)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        with contextlib.suppress(OSError):
            if f.stat().st_size > _ACCESS_MAX:
                os.replace(f, f.with_suffix(".log.1"))


async def access_log_task() -> None:
    while True:
        await asyncio.sleep(2)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_flush_logs)


def forget_app(app_id: int) -> None:
    stats.pop(app_id, None)
    _log_buf.pop(app_id, None)
    for p in (access_log_file(app_id), access_log_file(app_id).with_suffix(".log.1")):
        with contextlib.suppress(OSError):
            p.unlink()
    invalidate_cache(app_id)
