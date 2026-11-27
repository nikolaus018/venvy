"""Regression tests for the review of 2026-11-27 (docs/review-2026-11-27.md).
Needs no Venvy server: the proxy is driven as an ASGI app against a local
upstream. usage: t_review_2711.py <repo>"""
import asyncio, gzip, io, os, shutil, sys, tarfile, tempfile, time
from pathlib import Path

repo = sys.argv[1]
WORK = Path(tempfile.mkdtemp(prefix="venvy-2711-"))
os.chdir(WORK)                                   # Venvy keeps its data in the working directory
sys.path.insert(0, repo)
fails = []


def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c:
        fails.append(n)


from venvy.db import create_db_and_tables  # noqa: E402
create_db_and_tables()
from venvy import routing  # noqa: E402

# ── 1. route basic auth: limited guesses, a changed password is not cached ──
pw_hash = routing.hash_password("right-one")
rule = routing.Rule(host="h", prefix="/", app_id=1, route_id=7, users={"ann": pw_hash})


def hdr(user, pw):
    import base64
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


async def auth_checks():
    ok = await routing.basic_auth_ok(rule, hdr("ann", "right-one"), "1.1.1.1")
    check("basic auth: the right password gets in", ok)
    t0 = time.monotonic()
    await routing.basic_auth_ok(rule, hdr("nobody", "x"), "2.2.2.2")
    unknown = time.monotonic() - t0
    t0 = time.monotonic()
    await routing.basic_auth_ok(rule, hdr("ann", "wrong"), "2.2.2.2")
    wrong = time.monotonic() - t0
    # An unknown name costs a scrypt run too, so its answer time doesn't give it away.
    check("basic auth: an unknown user takes as long as a wrong password", unknown > wrong / 4, f"{unknown:.4f} vs {wrong:.4f}")
    results = [await routing.basic_auth_ok(rule, hdr("ann", f"guess{i}"), "3.3.3.3") for i in range(routing._AUTH_FAIL_MAX)]
    check("basic auth: wrong guesses fail", not any(results))
    check("basic auth: past the limit even the right password is refused from that address",
          not await routing.basic_auth_ok(routing.Rule(host="h", prefix="/", app_id=1, route_id=7, users={"ann": pw_hash}),
                                          hdr("ann", "right-one-but-not-cached"), "3.3.3.3")
          and not await routing.basic_auth_ok(rule, hdr("ann", "guess-more"), "3.3.3.3"))
    check("basic auth: other addresses are not locked out", await routing.basic_auth_ok(rule, hdr("ann", "right-one"), "4.4.4.4"))
    changed = routing.Rule(host="h", prefix="/", app_id=1, route_id=7, users={"ann": routing.hash_password("new-one")})
    check("basic auth: the old password stops working once changed (no 5-minute cache)",
          not await routing.basic_auth_ok(changed, hdr("ann", "right-one"), "1.1.1.1"))

asyncio.run(auth_checks())

# ── 2. the proxy cache doesn't keep answers that vary by more than encoding ──
cr = routing.Rule(host="h", prefix="/", app_id=1, cache_seconds=60)
check("cache: Vary: Accept-Encoding is fine", routing.cache_ttl(cr, {"cache-control": "public, max-age=30", "vary": "Accept-Encoding"}) == 30)
check("cache: Vary: Cookie is not cached", routing.cache_ttl(cr, {"cache-control": "public, max-age=30", "vary": "Accept-Encoding, Cookie"}) == 0)
check("cache: Vary: * is not cached", routing.cache_ttl(cr, {"cache-control": "public, max-age=30", "vary": "*"}) == 0)

# ── 3. an app that compresses by itself: its gzip answer is not served to a client without gzip ──
from aiohttp import web  # noqa: E402
from venvy import proxy, own_hosts  # noqa: E402


async def proxy_checks():
    async def page(request):
        body = b"hello " * 200
        headers = {"Cache-Control": "public, max-age=60", "Content-Type": "text/plain", "Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            return web.Response(body=gzip.compress(body), headers={**headers, "Content-Encoding": "gzip"})
        return web.Response(body=body, headers=headers)

    app = web.Application()
    app.router.add_get("/", page)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    r = routing.Rule(host="cache.example.com", prefix="/", app_id=42, cache_seconds=60, route_id=42)
    routing._rules = {"cache.example.com": [r]}
    routing._wild = []
    routing._targets = {42: routing.Target(42, "cache", [(42, port)], access_log=False)}
    routing._built_at = time.monotonic() + 3600          # keep this table: nothing to read it from
    own_hosts.is_own = lambda name: False

    mw = proxy.DomainProxyMiddleware(None)

    async def get(accept: str):
        sent = []
        headers = [(b"host", b"cache.example.com")]
        if accept:
            headers.append((b"accept-encoding", accept.encode()))
        scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"",
                 "headers": headers, "client": ("10.0.0.9", 5555), "server": ("127.0.0.1", 1), "scheme": "http"}
        msgs = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive():
            return msgs.pop(0) if msgs else {"type": "http.disconnect"}

        async def send(m):
            sent.append(m)
        await mw(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return dict((k.decode().lower(), v.decode()) for k, v in start["headers"]), body

    h1, b1 = await get("gzip, br")
    h2, b2 = await get("")
    h3, b3 = await get("br, gzip")
    check("proxy cache: first gzip answer from the app", h1.get("content-encoding") == "gzip" and h1.get("x-venvy-cache") == "MISS")
    check("proxy cache: a client without gzip gets plain text, not the cached gzip",
          "content-encoding" not in h2 and b2.startswith(b"hello"), str(h2))
    check("proxy cache: the same encodings again come from the cache", h3.get("x-venvy-cache") == "HIT"
          and gzip.decompress(b3) == b"hello " * 200, str(h3))
    await proxy.close()
    await runner.cleanup()

asyncio.run(proxy_checks())

# ── 4. UDP forwards: one upstream per client, however fast it sends ──
from venvy import forwards  # noqa: E402


async def udp_checks():
    loop = asyncio.get_running_loop()
    got = []

    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, data, addr):
            got.append((data, addr))
            self.t.sendto(data, addr)

    echo_t, _ = await loop.create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
    target = echo_t.get_extra_info("sockname")[1]
    front = forwards._UdpFront(1, target)
    front_t, _ = await loop.create_datagram_endpoint(lambda: front, local_addr=("127.0.0.1", 0))
    fport = front_t.get_extra_info("sockname")[1]
    replies = []

    class Client(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            replies.append(data)

    cli_t, _ = await loop.create_datagram_endpoint(Client, remote_addr=("127.0.0.1", fport))
    for i in range(5):
        cli_t.sendto(f"m{i}".encode())          # all before the upstream socket exists
    for _ in range(50):
        if len(replies) >= 5:
            break
        await asyncio.sleep(0.02)
    check("udp forward: every datagram sent before the upstream was up arrives", sorted(replies) == [f"m{i}".encode() for i in range(5)], str(replies))
    check("udp forward: one upstream socket for the client", len({a for _, a in got}) == 1 and len(front.ups) == 1, str(got))
    check("udp forward: no task left over", not front.tasks)
    front.close()
    cli_t.close()
    echo_t.close()

asyncio.run(udp_checks())

# ── 5. the public status page is not counted afresh on every request ──
from venvy import kv, telemetry  # noqa: E402
kv.put_json(telemetry.STATUS_KEY, {"enabled": True, "title": "S", "apps": []})
calls = []
real = telemetry._status_data
telemetry._status_data = lambda cfg: calls.append(1) or real(cfg)
for _ in range(20):
    telemetry.status_data()
check("status page: one count for a burst of requests", len(calls) == 1, str(len(calls)))
kv.put_json(telemetry.STATUS_KEY, {"enabled": True, "title": "T", "apps": []})
check("status page: a changed setting shows at once", telemetry.status_data()["title"] == "T")
telemetry._status_data = real

# ── 6. artifact archives are checked in full, off the event loop ──
from venvy.api import platform  # noqa: E402


def tgz(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, link in entries:
            ti = tarfile.TarInfo(name)
            if kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, link
                tf.addfile(ti)
            else:
                ti.size = 2
                tf.addfile(ti, io.BytesIO(b"hi"))
    p = WORK / f"a{time.time_ns()}.tar.gz"
    p.write_bytes(buf.getvalue())
    return p


def refused(p):
    try:
        platform._check_artifact(p)
        return False
    except ValueError:
        return True

check("artifact: a plain archive passes", not refused(tgz([("app.py", "file", ""), ("lib", "sym", "app.py")])))
check("artifact: escapes are refused", refused(tgz([("../x", "file", "")])) and refused(tgz([("l", "sym", "/etc")])))
bad = WORK / "broken.tar.gz"
bad.write_bytes(gzip.compress(b"x" * 1000)[:-8] + b"garbage!")
check("artifact: a broken archive is refused", refused(bad))

shutil.rmtree(WORK, ignore_errors=True)
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
