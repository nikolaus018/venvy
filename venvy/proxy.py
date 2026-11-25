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
from typing import Dict, Optional, Set

import aiohttp
from sqlmodel import Session, select
from starlette.requests import HTTPConnection

from venvy import own_hosts
from venvy.config import HTTP_CLIENT_LIMITS
from venvy.db import engine
from venvy.models import App

_TTL_SEC = 5.0
_cache: Dict[str, Optional[int]] = {}
_cache_at = 0.0
_refresh_lock: Optional[asyncio.Lock] = None
_session: Optional[aiohttp.ClientSession] = None
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
    global _cache_at
    _cache_at = 0.0


def _refresh_sync() -> None:
    global _cache, _cache_at
    try:
        with Session(engine) as s:
            rows = s.exec(select(App).where(App.domain != None)).all()  # noqa: E711
        _cache = {r.domain: (r.port if r.status == "running" else None) for r in rows if r.domain}
        _cache_at = time.monotonic()
    except Exception as exc:
        logging.warning(f"Could not read the app domains: {exc}")


async def _refresh_async() -> None:
    global _refresh_lock
    if _refresh_lock is None:
        _refresh_lock = asyncio.Lock()
    async with _refresh_lock:
        if time.monotonic() - _cache_at <= 0.5:
            return
        await asyncio.to_thread(_refresh_sync)


def lookup(host: str) -> tuple:
    """Synchronous lookup for tests/callers: (is_app_domain, port or None)."""
    age = time.monotonic() - _cache_at
    if age > _TTL_SEC:
        _refresh_sync()
    name = own_hosts.host_name(host)
    if name in _cache and not own_hosts.is_own(name):
        if _cache[name] is None and age > 0.5:
            _refresh_sync()
        return True, _cache[name]
    return False, None


async def lookup_async(host: str) -> tuple:
    """Async lookup avoiding synchronous SQLite I/O on the event loop."""
    age = time.monotonic() - _cache_at
    if age > _TTL_SEC:
        await _refresh_async()
    name = own_hosts.host_name(host)
    if name in _cache and not own_hosts.is_own(name):
        if _cache[name] is None and time.monotonic() - _cache_at > 0.5:
            await _refresh_async()
        return True, _cache[name]
    return False, None


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
        is_app, port = (await lookup_async(host)) if host else (False, None)
        if not is_app:
            await self.app(scope, receive, send)
            return
        # Never proxy back to Venvy's own listening port (prevents self-proxy loops).
        own_port = (scope.get("server") or (None, None))[1]
        if port and own_port and int(port) == int(own_port):
            port = None
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send, port)
        else:
            await self._http_request(scope, receive, send, port, host)

    async def _http_request(self, scope, receive, send, port, host) -> None:
        if not port:
            await _plain(send, 503, "This app is not running right now.")
            return
        path = _request_path(scope)
        if path is None:
            await _plain(send, 400, "Invalid request path.")
            return
        drop = set(_HOP) | set(_DROP) | _connection_hop_headers(scope["headers"])
        if not _trusted_proxy(scope):
            drop |= {"cf-connecting-ip", "cf-visitor"}
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]
                   if k.decode("latin-1").lower() not in drop]
        headers += [("X-Forwarded-For", _client_ip(scope)), ("X-Forwarded-Host", host),
                    ("X-Forwarded-Proto", _proto(scope))]
        qs = scope.get("query_string", b"").decode("latin-1")
        url = f"http://127.0.0.1:{port}{path}" + (f"?{qs}" if qs else "")

        async def body():
            while True:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                if msg.get("body"):
                    yield msg["body"]
                if not msg.get("more_body", False):
                    return

        cl_val = next((v for k, v in headers if k.lower() == "content-length"), None)
        has_body = scope["method"] not in ("GET", "HEAD", "OPTIONS") or (cl_val is not None and cl_val != "0")
        req_data = None
        if has_body:
            buffered = False
            if cl_val is not None and cl_val.isdigit() and int(cl_val) <= 16 * 1024 * 1024:
                # Buffer bounded Content-Length bodies so HTTP/1.0 upstream apps
                # (which do not understand Transfer-Encoding: chunked) still see Content-Length.
                buf = bytearray()
                over = False
                async for chunk in body():
                    buf.extend(chunk)
                    if len(buf) > 16 * 1024 * 1024:
                        over = True
                        break
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
        started = False
        try:
            async with _http().request(scope["method"], url, headers=headers, data=req_data,
                                       allow_redirects=False) as resp:
                resp_hop = _HOP | _connection_hop_headers(resp.raw_headers)
                out = [(k.lower(), v) for k, v in resp.raw_headers if k.decode("latin-1").lower() not in resp_hop]
                await send({"type": "http.response.start", "status": resp.status, "headers": out})
                started = True
                async for chunk in resp.content.iter_any():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception as exc:
            if not isinstance(exc, (aiohttp.ClientConnectionError, asyncio.TimeoutError, ConnectionRefusedError)):
                logging.warning(f"Proxy to port {port} failed: {exc}")
            if started:
                # The status line is out: all that is left is to end the response.
                with contextlib.suppress(Exception):
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await _plain(send, 502, "The app did not answer.")

    async def _websocket(self, scope, receive, send, port) -> None:
        if not port:
            with contextlib.suppress(Exception):
                await receive()
            await send({"type": "websocket.close", "code": 1013})
            return
        path = _request_path(scope)
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
            elif kl in ("host", "origin", "user-agent", "authorization"):
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
