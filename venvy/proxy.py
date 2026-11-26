"""Hostname routing: an app that has a domain is served on it.

Requests that reach Venvy's own port with the app's domain in the Host header
are passed to the app (plain HTTP and WebSocket), without Venvy's login — the
app is public on that name, as it would be on its own port. Point the domain
at this server (or a Cloudflare tunnel / a TLS-terminating proxy in front of
it); Venvy does not speak TLS itself.

Only exact matches of an app's domain are proxied, so Venvy's own UI is never
reachable under an app's name and the reverse — nor is a name Venvy itself has
been signed in to under (own_hosts.py), whatever domain an app was given.

X-Venvy-Proxy-Secret and client-forged forwarding headers are not passed on:
the secret would let the app pass for the tunnel, and the X-Forwarded-* it
gets are Venvy's own."""
import asyncio
import contextlib
import logging
import os
import time
import zlib
from typing import Dict, List, Optional, Set

import aiohttp
from sqlmodel import Session, select
from starlette.requests import HTTPConnection

from venvy import own_hosts, routing
from venvy.config import HTTP_CLIENT_LIMITS

_session: Optional[aiohttp.ClientSession] = None
_GZIP_TYPES = ("text/", "application/json", "application/javascript", "application/xml", "image/svg+xml",
               "application/manifest+json", "application/x-javascript", "application/ld+json")
_WS_MAX_MSG_BYTES = max(1, int(os.getenv("VENVY_PROXY_WS_MAX_MB", "16"))) * 1024 * 1024

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "proxy-connection"}

# Request headers that never reach the app (see the module docstring).
_DROP = {
    "x-venvy-proxy-secret",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-real-ip",
    "forwarded",
}


def invalidate() -> None:
    routing.invalidate()


def lookup(host: str, path: str = "/") -> tuple:
    """Synchronous lookup for tests/callers: (is_app_domain, port or None)."""
    routing._build()
    rule = routing.match_sync(host, path)
    if rule is None:
        return False, None
    ups = routing.upstreams(rule.app_id)
    return True, (ups[0][1] if ups else None)


def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=300),
            connector=aiohttp.TCPConnector(limit=200),
            **HTTP_CLIENT_LIMITS,
        )
    return _session


async def close() -> None:
    if _session and not _session.closed:
        await _session.close()


async def _plain(send, status: int, text: str) -> None:
    body = (f"<!doctype html><meta charset=utf-8><title>{status}</title>"
            f"<body style='font:16px system-ui;margin:4rem auto;max-width:30rem;color:#444'><h1>{status}</h1><p>{text}</p>").encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def _proto(scope) -> str:
    """How the browser reached us: through the tunnel that is HTTPS, though
    cloudflared talks plain HTTP to Venvy (see auth.request_is_https)."""
    from venvy.auth import request_is_https
    try:
        return "https" if request_is_https(HTTPConnection(scope)) else "http"
    except Exception:
        return scope.get("scheme", "http")


def _client_ip(scope) -> str:
    from venvy.auth import _resolve_client_ip
    try:
        return _resolve_client_ip(HTTPConnection(scope))
    except Exception:
        return (scope.get("client") or ("",))[0]


def _trusted_proxy(scope) -> bool:
    from venvy.auth import _from_trusted_proxy
    try:
        return _from_trusted_proxy(HTTPConnection(scope))
    except Exception:
        return False


def _connection_hop_headers(raw_headers) -> Set[str]:
    """Headers nominated as hop-by-hop via RFC 7230 Connection header."""
    extra: Set[str] = set()
    for k, v in raw_headers:
        if k.lower() == b"connection":
            for part in v.decode("latin-1").split(","):
                name = part.strip().lower()
                if name:
                    extra.add(name)
    return extra


def _request_path(scope) -> Optional[str]:
    raw = scope.get("raw_path")
    if raw:
        path = raw.decode("latin-1")
    else:
        path = scope.get("path") or "/"
    if not path.startswith("/"):
        return None
    return path


class DomainProxyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        host = ""
        for k, v in scope["headers"]:
            if k == b"host":
                host = v.decode("latin-1")
                break
        raw_path = _request_path(scope) or "/"
        # Matched on the normalised path, so an encoded or padded one can't
        # slip past a path route (and its controls) to the host's "/" rule.
        mpath = routing.normalize_path(raw_path)
        rule = (await routing.match(host, mpath)) if host else None
        if rule is None:
            await self.app(scope, receive, send)
            return
        if routing.path_sensitive(host, rule):
            # The app gets the path the route was chosen with — and none it
            # could read differently (an encoded slash or a backslash).
            if routing.ambiguous_path(raw_path):
                await self._refuse(scope, receive, send, 400, "That path is not allowed here.")
                return
            path = mpath
        else:
            path = raw_path
        ip = _client_ip(scope)
        hdrs = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if not routing.ip_allowed(rule, ip):
            await self._refuse(scope, receive, send, 403, "Your address is not allowed here.")
            return
        if not await routing.basic_auth_ok(rule, hdrs.get("authorization")):
            await self._refuse(scope, receive, send, 401, "Sign in to continue.",
                               [(b"www-authenticate", b'Basic realm="restricted", charset="UTF-8"')])
            return
        if not routing.rate_ok(rule, ip):
            await self._refuse(scope, receive, send, 429, "Too many requests — slow down a little.",
                               [(b"retry-after", b"60")])
            return
        if rule.kind == "redirect":
            await self._redirect(scope, receive, send, rule, path)
            return
        # Never proxy back to Venvy's own listening port (prevents self-proxy loops).
        own_port = (scope.get("server") or (None, None))[1]
        ups = [u for u in routing.upstreams(rule.app_id) if not (own_port and int(u[1]) == int(own_port))]
        if not ups and routing.age() > 0.5:
            # Just started (or given its port) a moment ago: look again before saying it is down.
            routing.invalidate()
            rule = (await routing.match(host, mpath)) or rule
            ups = [u for u in routing.upstreams(rule.app_id) if not (own_port and int(u[1]) == int(own_port))]
        upath = path
        if rule.strip and rule.prefix not in ("", "/"):
            p = rule.prefix.rstrip("/")
            upath = path[len(p):] or "/"
            if not upath.startswith("/"):
                upath = "/" + upath
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send, ups[0][1] if ups else None, upath,
                                  drop_auth=bool(rule.users))
        else:
            await self._http_request(scope, receive, send, ups, host, rule, upath, ip, hdrs)

    async def _refuse(self, scope, receive, send, status: int, text: str, extra=None) -> None:
        if scope["type"] == "websocket":
            with contextlib.suppress(Exception):
                await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        body = (f"<!doctype html><meta charset=utf-8><title>{status}</title>"
                f"<body style='font:16px system-ui;margin:4rem auto;max-width:30rem;color:#444'><h1>{status}</h1><p>{text}</p>").encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store")] + list(extra or [])})
        await send({"type": "http.response.body", "body": body})

    async def _redirect(self, scope, receive, send, rule, path: str) -> None:
        to = (rule.redirect_to or "").strip()
        qs = scope.get("query_string", b"").decode("latin-1")
        if "://" in to:
            loc = to
        else:
            loc = f"{_proto(scope)}://{to}{path}" + (f"?{qs}" if qs else "")
        if scope["type"] == "websocket":
            await self._refuse(scope, receive, send, 400, "")
            return
        code = rule.code if rule.code in (301, 302, 303, 307, 308) else 301
        await send({"type": "http.response.start", "status": code, "headers": [
            (b"location", loc.encode("latin-1", errors="replace")), (b"content-length", b"0")]})
        await send({"type": "http.response.body", "body": b""})

    async def _http_request(self, scope, receive, send, ups, host, rule, path, ip, hdrs) -> None:
        t0 = time.monotonic()
        tgt = routing.target(rule.app_id)
        log_it = bool(tgt and tgt.access_log)
        method = scope["method"]
        status_sent = [0, 0]           # status, bytes

        async def counted_send(msg):
            if msg["type"] == "http.response.start":
                status_sent[0] = msg["status"]
            elif msg["type"] == "http.response.body":
                status_sent[1] += len(msg.get("body") or b"")
            await send(msg)
        try:
            await self._http_inner(scope, receive, counted_send, ups, host, rule, path, ip, hdrs)
        finally:
            routing.record(rule.app_id, method, path, status_sent[0] or 499, status_sent[1], time.monotonic() - t0,
                           ip, host, hdrs.get("user-agent", ""), log_it)

    async def _http_inner(self, scope, receive, send, ups, host, rule, path, ip, hdrs) -> None:
        if not ups:
            await _plain(send, 503, "This app is not running right now.")
            return
        drop = set(_HOP) | set(_DROP) | _connection_hop_headers(scope["headers"])
        if not _trusted_proxy(scope):
            drop |= {"cf-connecting-ip", "cf-visitor"}
        if rule.users:
            drop.add("authorization")            # Venvy's basic auth is not the app's business
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]
                   if k.decode("latin-1").lower() not in drop]
        headers += [("X-Forwarded-For", ip), ("X-Forwarded-Host", host),
                    ("X-Forwarded-Proto", _proto(scope))]
        if rule.strip and rule.prefix not in ("", "/"):
            headers.append(("X-Forwarded-Prefix", rule.prefix.rstrip("/")))
        qs = scope.get("query_string", b"").decode("latin-1")
        method = scope["method"]

        cl_val = next((v for k, v in headers if k.lower() == "content-length"), None)
        if rule.max_body and cl_val is not None and cl_val.isdigit() and int(cl_val) > rule.max_body:
            await _plain(send, 413, "The request is too large.")
            return

        cache_key = None
        if rule.cache_seconds and method in ("GET", "HEAD") and "authorization" not in hdrs:
            gz = "gzip" in hdrs.get("accept-encoding", "") and rule.gzip
            cache_key = (rule.app_id, host.lower(), path, qs, gz)
            hit = routing.cache_get(cache_key)
            if hit is not None:
                status, out_headers, body_bytes = hit
                await send({"type": "http.response.start", "status": status,
                            "headers": out_headers + [(b"x-venvy-cache", b"HIT")]})
                await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body_bytes})
                return

        received = [0]

        async def body():
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                if msg.get("body"):
                    received[0] += len(msg["body"])
                    if rule.max_body and received[0] > rule.max_body:
                        raise _TooLarge()
                    yield msg["body"]
                if not msg.get("more_body", False):
                    return

        has_body = method not in ("GET", "HEAD", "OPTIONS") or (cl_val is not None and cl_val != "0")
        req_data = None
        if has_body:
            buffered = False
            if cl_val is not None and cl_val.isdigit() and int(cl_val) <= 16 * 1024 * 1024:
                # Buffer bounded Content-Length bodies so HTTP/1.0 upstream apps
                # (which do not understand Transfer-Encoding: chunked) still see Content-Length.
                buf = bytearray()
                over = False
                try:
                    async for chunk in body():
                        buf.extend(chunk)
                        if len(buf) > 16 * 1024 * 1024:
                            over = True
                            break
                except _TooLarge:
                    await _plain(send, 413, "The request is too large.")
                    return
                if not over:
                    req_data = bytes(buf)
                    headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
                    headers.append(("Content-Length", str(len(req_data))))
                    buffered = True
                else:
                    async def _tail(first=bytes(buf)):
                        yield first
                        async for c in body():
                            yield c
                    headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
                    req_data = _tail()
                    buffered = True
            if not buffered:
                # Streaming an async generator uses chunked transfer-encoding in aiohttp;
                # strip incoming Content-Length to avoid conflicting CL + TE headers.
                headers = [(k, v) for k, v in headers if k.lower() != "content-length"]
                req_data = body()
        # Another replica is tried only while nothing of the body has been sent on.
        retryable = not has_body or isinstance(req_data, (bytes, type(None)))
        started = False
        for attempt, (_pid, port) in enumerate(ups if retryable else ups[:1]):
            url = f"http://127.0.0.1:{port}{path}" + (f"?{qs}" if qs else "")
            routing.inflight[_pid] += 1
            try:
                async with _http().request(method, url, headers=headers, data=req_data,
                                           allow_redirects=False) as resp:
                    resp_hop = _HOP | _connection_hop_headers(resp.raw_headers)
                    out = [(k.lower(), v) for k, v in resp.raw_headers if k.decode("latin-1").lower() not in resp_hop]
                    lower = {k.decode("latin-1"): v.decode("latin-1") for k, v in out}
                    for hk, hv in rule.headers:
                        out = [(k, v) for k, v in out if k.decode("latin-1") != hk.lower()]
                        out.append((hk.lower().encode("latin-1"), hv.encode("latin-1", errors="replace")))
                    ctype = lower.get("content-type", "")
                    compress = (rule.gzip and "gzip" in hdrs.get("accept-encoding", "")
                                and "content-encoding" not in lower and resp.status == 200
                                and method != "HEAD" and ctype.startswith(_GZIP_TYPES)
                                and "content-range" not in lower)
                    ttl = routing.cache_ttl(rule, lower) if cache_key is not None and resp.status == 200 else 0
                    if compress:
                        out = [(k, v) for k, v in out if k not in (b"content-length", b"etag")]
                        out += [(b"content-encoding", b"gzip"), (b"vary", b"Accept-Encoding")]
                    await send({"type": "http.response.start", "status": resp.status,
                                "headers": out + ([(b"x-venvy-cache", b"MISS")] if cache_key is not None else [])})
                    started = True
                    zc = zlib.compressobj(6, zlib.DEFLATED, 31) if compress else None
                    keep: Optional[bytearray] = bytearray() if ttl > 0 else None
                    async for chunk in resp.content.iter_any():
                        if zc is not None:
                            chunk = zc.compress(chunk)
                            if not chunk:
                                continue
                        if keep is not None:
                            keep.extend(chunk)
                            if len(keep) > 2 * 1024 * 1024:
                                keep = None
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    tail = zc.flush() if zc is not None else b""
                    if keep is not None:
                        keep.extend(tail)
                        routing.cache_put(cache_key, ttl, resp.status, out, bytes(keep))
                    await send({"type": "http.response.body", "body": tail, "more_body": False})
                return
            except _TooLarge:
                if not started:
                    await _plain(send, 413, "The request is too large.")
                return
            except Exception as exc:
                if started:
                    # The status line is out: all that is left is to end the response.
                    with contextlib.suppress(Exception):
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                if not isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError, ConnectionRefusedError)):
                    logging.warning(f"Proxy to port {port} failed: {exc}")
                    break
            finally:
                routing.inflight[_pid] -= 1
                if routing.inflight[_pid] <= 0:
                    routing.inflight.pop(_pid, None)
        await _plain(send, 502, "The app did not answer.")

    async def _websocket(self, scope, receive, send, port, path: Optional[str] = None,
                         drop_auth: bool = False) -> None:
        if not port:
            with contextlib.suppress(Exception):
                await receive()
            await send({"type": "websocket.close", "code": 1013})
            return
        path = path or _request_path(scope)
        if path is None:
            with contextlib.suppress(Exception):
                await receive()
            await send({"type": "websocket.close", "code": 1002})
            return
        qs = scope.get("query_string", b"").decode("latin-1")
        url = f"ws://127.0.0.1:{port}{path}" + (f"?{qs}" if qs else "")
        protocols = [p for p in scope.get("subprotocols", [])]
        fwd: Dict[str, str] = {}
        cookies = []
        for k, v in scope["headers"]:
            kl = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            if kl == "cookie":
                cookies.append(val)
            elif kl in ("host", "origin", "user-agent") or (kl == "authorization" and not drop_auth):
                # A route's basic auth is Venvy's: its password is not the app's business.
                fwd[kl] = val
        if cookies:
            fwd["cookie"] = "; ".join(cookies)
        fwd["X-Forwarded-For"] = _client_ip(scope)
        if "host" in fwd:
            fwd["X-Forwarded-Host"] = fwd["host"]
        fwd["X-Forwarded-Proto"] = _proto(scope)
        try:
            upstream = await _http().ws_connect(
                url, headers=fwd, protocols=protocols,
                max_msg_size=_WS_MAX_MSG_BYTES, autoclose=False, autoping=False,
            )
        except Exception as exc:
            logging.warning(f"WebSocket proxy to 127.0.0.1:{port}{path} failed: {exc}")
            await receive()           # the connect message
            await send({"type": "websocket.close", "code": 1011})
            return
        await receive()               # websocket.connect
        await send({"type": "websocket.accept", "subprotocol": upstream.protocol or None})

        async def client_to_upstream() -> None:
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    await upstream.send_str(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send_bytes(msg["bytes"])

        async def upstream_to_client() -> None:
            async for m in upstream:
                if m.type == aiohttp.WSMsgType.TEXT:
                    await send({"type": "websocket.send", "text": m.data})
                elif m.type == aiohttp.WSMsgType.BINARY:
                    await send({"type": "websocket.send", "bytes": m.data})
                elif m.type == aiohttp.WSMsgType.PING:
                    await upstream.pong(m.data)
                elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    return

        tasks = [asyncio.ensure_future(client_to_upstream()), asyncio.ensure_future(upstream_to_client())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await upstream.close()
            code = upstream.close_code
            if not code or not 1000 <= code < 5000 or code in (1004, 1005, 1006, 1015):     # not sendable
                code = 1000
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": code})


class _TooLarge(Exception):
    pass
