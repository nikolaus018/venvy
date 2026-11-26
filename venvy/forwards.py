"""Raw TCP and UDP ports passed straight to an app (game servers, MQTT, SMTP,
database clients — anything that is not HTTP).

Each PortForward listens on listen_host:listen_port and relays to the app's
target port on 127.0.0.1. TCP is relayed connection by connection; UDP keeps
one upstream socket per client address (closed after two idle minutes).
Ports below 1024 need Venvy to run as root (it does, with per-app users).
"""
import asyncio
import contextlib
import logging
import time
from typing import Dict, Optional, Tuple

from sqlmodel import Session, select

from venvy.db import engine
from venvy.models import PortForward

_servers: Dict[int, object] = {}          # forward id -> asyncio Server or (transport, protocol)
_config: Dict[int, Tuple] = {}
_errors: Dict[int, str] = {}
_counts: Dict[int, Dict[str, int]] = {}
_UDP_IDLE = 120.0


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, fid: int, key: str) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            _counts.setdefault(fid, {}).setdefault(key, 0)
            _counts[fid][key] += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def _start_tcp(fwd: PortForward):
    fid, target = fwd.id, fwd.target_port

    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        _counts.setdefault(fid, {}).setdefault("connections", 0)
        _counts[fid]["connections"] += 1
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", target), 10)
        except Exception:
            cw.close()
            return
        await asyncio.gather(_pipe(cr, uw, fid, "bytes_in"), _pipe(ur, cw, fid, "bytes_out"))

    return await asyncio.start_server(handle, fwd.listen_host, fwd.listen_port, reuse_address=True)


class _UdpUpstream(asyncio.DatagramProtocol):
    def __init__(self, front, client):
        self.front, self.client, self.transport, self.seen = front, client, None, time.monotonic()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.seen = time.monotonic()
        if self.front.transport is not None:
            self.front.transport.sendto(data, self.client)


class _UdpFront(asyncio.DatagramProtocol):
    def __init__(self, fid: int, target: int):
        self.fid, self.target, self.transport = fid, target, None
        self.ups: Dict[tuple, _UdpUpstream] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        up = self.ups.get(addr)
        _counts.setdefault(self.fid, {}).setdefault("datagrams", 0)
        _counts[self.fid]["datagrams"] += 1
        if up is not None and up.transport is not None:
            up.seen = time.monotonic()
            up.transport.sendto(data)
            return
        asyncio.ensure_future(self._open(addr, data))

    async def _open(self, addr, first: bytes) -> None:
        loop = asyncio.get_running_loop()
        if len(self.ups) > 4096:
            return
        _t, up = await loop.create_datagram_endpoint(lambda: _UdpUpstream(self, addr),
                                                     remote_addr=("127.0.0.1", self.target))
        self.ups[addr] = up
        up.transport.sendto(first)

    def reap(self) -> None:
        now = time.monotonic()
        for addr, up in list(self.ups.items()):
            if now - up.seen > _UDP_IDLE:
                with contextlib.suppress(Exception):
                    up.transport.close()
                self.ups.pop(addr, None)

    def close(self) -> None:
        for up in self.ups.values():
            with contextlib.suppress(Exception):
                up.transport.close()
        self.ups.clear()
        if self.transport is not None:
            self.transport.close()


async def _start_udp(fwd: PortForward):
    loop = asyncio.get_running_loop()
    front = _UdpFront(fwd.id, fwd.target_port)
    await loop.create_datagram_endpoint(lambda: front, local_addr=(fwd.listen_host, fwd.listen_port), reuse_port=False)
    return front


async def _stop(fid: int) -> None:
    srv = _servers.pop(fid, None)
    _config.pop(fid, None)
    if srv is None:
        return
    if isinstance(srv, _UdpFront):
        srv.close()
    else:
        srv.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), 5)


async def sync() -> None:
    """Listen on exactly the enabled forwards (restarting changed ones)."""
    with Session(engine) as s:
        rows = {f.id: f for f in s.exec(select(PortForward)).all() if f.enabled}
    for fid in [f for f in _servers if f not in rows or _config.get(f) != _key(rows[f])]:
        await _stop(fid)
    for fid, fwd in rows.items():
        if fid in _servers:
            continue
        try:
            _servers[fid] = await (_start_tcp(fwd) if fwd.protocol == "tcp" else _start_udp(fwd))
            _config[fid] = _key(fwd)
            _errors.pop(fid, None)
        except OSError as exc:
            _errors[fid] = exc.strerror or str(exc)
            logging.warning("Port forward %s:%s could not listen: %s", fwd.listen_host, fwd.listen_port, exc)


def _key(f: PortForward) -> tuple:
    return (f.protocol, f.listen_host, f.listen_port, f.target_port)


def state(fid: int) -> dict:
    return {"listening": fid in _servers, "error": _errors.get(fid), "counters": dict(_counts.get(fid, {}))}


async def forwards_task() -> None:
    while True:
        with contextlib.suppress(Exception):
            await sync()
        for srv in list(_servers.values()):
            if isinstance(srv, _UdpFront):
                srv.reap()
        await asyncio.sleep(15)


async def close_all() -> None:
    for fid in list(_servers):
        await _stop(fid)
