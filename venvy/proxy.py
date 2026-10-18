"""Hostname routing: an app that has a domain is served on it.

Requests that reach Venvy's own port with the app's domain in the Host header
are passed to the app (plain HTTP and WebSocket), without Venvy's login — the
app is public on that name, as it would be on its own port. Point the domain
at this server (or a Cloudflare tunnel / a TLS-terminating proxy in front of
it); Venvy does not speak TLS itself.

Only exact matches of an app's domain are proxied, so Venvy's own UI is never
reachable under an app's name and the reverse."""
import asyncio
import contextlib
import logging
import time
from typing import Dict, Optional

import aiohttp
from sqlmodel import Session, select
from starlette.requests import HTTPConnection

from venvy.db import engine
from venvy.models import App

_TTL_SEC = 5.0
_cache: Dict[str, Optional[int]] = {}
_cache_at = 0.0
_session: Optional[aiohttp.ClientSession] = None

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "proxy-connection"}


def invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


def _refresh() -> None:
    global _cache, _cache_at
    try:
        with Session(engine) as s:
            rows = s.exec(select(App).where(App.domain != None)).all()  # noqa: E711
        _cache = {r.domain: (r.port if r.status == "running" else None) for r in rows if r.domain}
        _cache_at = time.monotonic()
    except Exception as exc:
        logging.warning(f"Could not read the app domains: {exc}")


def lookup(host: str) -> tuple:
    """(is_app_domain, port or None) for a Host header value."""
    age = time.monotonic() - _cache_at
    if age > _TTL_SEC:
        _refresh()
    name = host.split(":")[0].strip().lower().rstrip(".")
    if name in _cache:
        if _cache[name] is None and age > 0.5:
            _refresh()          # not running a moment ago: look again, it may have started
        return True, _cache[name]
    return False, None


def _http() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            auto_decompress=False, timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=300),
            connector=aiohttp.TCPConnector(limit=200))
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


def _client_ip(scope) -> str:
    from venvy.auth import _resolve_client_ip
    try:
        return _resolve_client_ip(HTTPConnection(scope))
    except Exception:
        return (scope.get("client") or ("",))[0]


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
        is_app, port = lookup(host) if host else (False, None)
        if not is_app:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send, port)
        else:
            await self._http_request(scope, receive, send, port, host)

    async def _http_request(self, scope, receive, send, port, host) -> None:
        if not port:
            await _plain(send, 503, "This app is not running right now.")
            return
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"] if k.decode("latin-1") not in _HOP]
        headers = [(k, v) for k, v in headers if k != "x-forwarded-for"]
        headers += [("X-Forwarded-For", _client_ip(scope)), ("X-Forwarded-Host", host),
                    ("X-Forwarded-Proto", scope.get("scheme", "http"))]
        path = scope.get("raw_path", scope["path"].encode()).decode("latin-1") or "/"
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

        has_body = scope["method"] not in ("GET", "HEAD", "OPTIONS") or any(k == "content-length" and v != "0" for k, v in headers)
        try:
            async with _http().request(scope["method"], url, headers=headers, data=body() if has_body else None,
                                       allow_redirects=False) as resp:
                out = [(k.lower(), v) for k, v in resp.raw_headers if k.decode("latin-1").lower() not in _HOP]
                await send({"type": "http.response.start", "status": resp.status, "headers": out})
                async for chunk in resp.content.iter_any():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, ConnectionRefusedError):
            await _plain(send, 502, "The app did not answer.")
        except Exception as exc:
            logging.warning(f"Proxy to port {port} failed: {exc}")
            await _plain(send, 502, "The app did not answer.")

    async def _websocket(self, scope, receive, send, port) -> None:
        if not port:
            await send({"type": "websocket.close", "code": 1013})
            return
        qs = scope.get("query_string", b"").decode("latin-1")
        url = f"ws://127.0.0.1:{port}{scope['path']}" + (f"?{qs}" if qs else "")
        protocols = [p for p in scope.get("subprotocols", [])]
        fwd = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]
               if k.decode("latin-1") in ("cookie", "origin", "user-agent", "authorization")}
        fwd["X-Forwarded-For"] = _client_ip(scope)
        try:
            upstream = await _http().ws_connect(url, headers=fwd, protocols=protocols, max_msg_size=0, autoclose=False, autoping=False)
        except Exception as exc:
            logging.warning(f"WebSocket proxy to {url} failed: {exc}")
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
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 1000})
