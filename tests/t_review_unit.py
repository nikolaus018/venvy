"""Findings of docs/review-2026-11-26.md that need no server: port ownership
(venvy/ports.py), the free-port search, deploy health's port probe, the
compose rules (checked after the old containers are gone; limits; container
names), egress rules for container apps and preview domains."""
import asyncio, json, os, socket, sys, tempfile, threading, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

repo = sys.argv[1]
WORK = Path(tempfile.mkdtemp(prefix="venvy-review-unit-"))
os.chdir(WORK)
sys.path.insert(0, repo)
fails = []


def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c:
        fails.append(n)


from sqlmodel import Session  # noqa: E402
from venvy.db import create_db_and_tables, engine  # noqa: E402
from venvy.models import App, Route  # noqa: E402
from venvy import compose_guard, monitor, ports, previews, process  # noqa: E402
from venvy.api.apps import apply_spec_fields  # noqa: E402

create_db_and_tables()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── ports.claimed ───────────────────────────────────────────────────────────
a = types.SimpleNamespace(port=8100, manual_ports="[8101, true, 70000]", detected_ports="[8102]")
check("claimed: port, manual and seen ports, nothing invalid", ports.claimed(a) == {8100, 8101, 8102}, str(ports.claimed(a)))
check("claimed without seen ports", ports.claimed(a, seen=False) == {8100, 8101})

# ── ports.listening: a compose app's ports are its containers' ──────────────
_real_own = process._own_containers_sync
try:
    def _fake_own(app):
        if app.runtime != "compose":
            return []
        return [{"NetworkSettings": {"Ports": {"80/tcp": [{"HostPort": "9711"}], "443/tcp": [{"HostPort": "9712"}]}}}]
    process._own_containers_sync = _fake_own
    capp = types.SimpleNamespace(id=7, runtime="compose", pid=None, port=9711)
    check("listening: every port a compose app's containers publish", ports.listening(capp) == [9711, 9712],
          str(ports.listening(capp)))
    check("listening: a process app without a live process holds nothing",
          ports.listening(types.SimpleNamespace(id=8, runtime="python", pid=None)) == [])
finally:
    process._own_containers_sync = _real_own

# ── ports.holder ────────────────────────────────────────────────────────────
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen()
held = srv.getsockname()[1]
me = types.SimpleNamespace(id=1, runtime="python", pid=os.getpid(), pid_start=None)
check("holder: a port this process listens on is 'self'", ports.holder(held, me) == "self", str(ports.holder(held, me)))
check("holder: ... and someone else's for another app",
      ports.holder(held, types.SimpleNamespace(id=2, runtime="python", pid=None)) == "other")
idle = free_port()
check("holder: nothing listening is None", ports.holder(idle, me) is None)
srv.close()

# ── finding 4: the free-port search skips other apps' ports ─────────────────
base = free_port()
while any(not process._verify_port_free(p) for p in (base, base + 1, base + 2)):
    base = free_port()
with Session(engine) as s:
    s.add(App(user_id=1, name="Alpha", slug="alpha", app_dir=str(WORK / "alpha"), venv_path="", entrypoint="main.py",
              port=base, status="running"))
    s.commit()
process._recent_allocated_ports.clear()
got = process.find_free_port(base, base + 3)
check("a port another app has (but doesn't listen on) is not handed out", got == base + 1, f"got {got}, Alpha has {base}")

# ── finding 2: deploy health's probe of a web app without a health check ────
def _http(status):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *x):
            pass
    h = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=h.serve_forever, daemon=True).start()
    return h


def _closer():
    """Accepts and closes at once, as docker-proxy does while the container isn't up."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()

    def run():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            c.close()
    threading.Thread(target=run, daemon=True).start()
    return s


def _silent():
    """Accepts and waits for the client to speak its protocol (a database)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    keep = []

    def run():
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            keep.append(c)
    threading.Thread(target=run, daemon=True).start()
    return s


async def _probes():
    out = {}
    for name, status in (("ok", 200), ("404", 404), ("bad gateway", 502), ("unavailable", 503)):
        h = _http(status)
        out[name] = (await monitor.probe_up(h.server_address[1]))["ok"]
        h.shutdown()
    c = _closer()
    out["closes at once"] = (await monitor.probe_up(c.getsockname()[1]))["ok"]
    c.close()
    q = _silent()
    out["tcp service"] = (await monitor.probe_up(q.getsockname()[1]))["ok"]
    q.close()
    out["refused"] = (await monitor.probe_up(free_port()))["ok"]
    await monitor.close_probe_session()
    return out

if hasattr(monitor, "probe_up"):
    res = asyncio.run(_probes())
    check("probe_up: an HTTP answer counts, 404 too", res["ok"] and res["404"], str(res))
    check("probe_up: 502 / 503 do not (a proxy in front of an app that isn't up)",
          not res["bad gateway"] and not res["unavailable"], str(res))
    check("probe_up: a connection closed at once does not (docker-proxy, no container)", not res["closes at once"], str(res))
    check("probe_up: a TCP service that waits for its client does", res["tcp service"], str(res))
    check("probe_up: nothing listening does not", not res["refused"], str(res))
else:
    check("probe_up exists", False)

# ── finding 5: compose is checked after the old containers are gone ─────────
order = []
_real_cleanup, _real_pin = process._docker_cleanup, compose_guard.pinned_argv
try:
    async def _fake_cleanup(app):
        order.append("cleanup")

    def _fake_pin(app, argv, env=None):
        order.append("check")
        return argv
    process._docker_cleanup = _fake_cleanup
    compose_guard.pinned_argv = _fake_pin
    spec = types.SimpleNamespace(runtime="compose", id=None)
    if hasattr(process, "_container_argv"):
        asyncio.run(process._container_argv(spec, 12, ["docker", "compose", "-f", "x", "--project-directory", "/", "up"],
                                            {}))
    check("compose: old containers removed before the file is checked", order[:2] == ["cleanup", "check"], str(order))
finally:
    process._docker_cleanup, compose_guard.pinned_argv = _real_cleanup, _real_pin

# ── finding 6: limits reach a compose app's containers ──────────────────────
proj = {"services": {"web": {"image": "x"}, "db": {"image": "y", "mem_limit": 64 * 1024 * 1024, "cpus": 0.25}}}
limited = types.SimpleNamespace(memory_limit_mb=256, cpu_limit_percent=50, pids_limit=100)
if hasattr(compose_guard, "apply_limits"):
    compose_guard.apply_limits(proj, limited)
web, db = proj["services"]["web"], proj["services"]["db"]
check("compose limits: set on a service without its own", web.get("mem_limit") == 256 * 1024 * 1024
      and web.get("cpus") == 0.5 and web.get("pids_limit") == 100, str(web))
check("compose limits: a service's own lower limit stays", db.get("mem_limit") == 64 * 1024 * 1024 and db.get("cpus") == 0.25,
      str(db))
proj2 = {"services": {"web": {"image": "x", "mem_limit": 2 * 1024 ** 3}}}
if hasattr(compose_guard, "apply_limits"):
    compose_guard.apply_limits(proj2, limited)
check("compose limits: a higher one of its own is lowered to the app's", proj2["services"]["web"]["mem_limit"] == 256 * 1024 * 1024,
      str(proj2))

# ── finding 6: egress rules that can't be enforced are refused ──────────────
def egress(runtime, policy, port=None):
    row = App(user_id=1, name="e", slug="e", app_dir="/x", venv_path="", entrypoint="img", runtime=runtime, port=port)
    try:
        apply_spec_fields(row, {"egress_policy": policy}, admin=True)
        return True
    except ValueError:
        return False

check("egress deny/allowlist refused for compose apps", not egress("compose", "deny") and not egress("compose", "allowlist"))
check("egress deny refused for a docker app with a port", not egress("docker", "deny", 8200))
check("egress deny kept for a docker app without one, and for processes",
      egress("docker", "deny") and egress("python", "deny", 8200) and egress("compose", "allow"))

# ── finding 10: container names stay the app's ──────────────────────────────
def refused(project, name):
    try:
        compose_guard.check(project, [WORK], name)
        return False
    except compose_guard.ComposeRefused:
        return True
    except TypeError:
        return False

check("compose: another app's container name refused",
      refused({"services": {"w": {"image": "x", "container_name": "venvy-app-5"}}}, "venvy-app-7"))
check("compose: a name of the app's own is fine",
      not refused({"services": {"w": {"image": "x", "container_name": "venvy-app-7-cache"}}}, "venvy-app-7"))

# ── finding 9: preview domains don't collide ────────────────────────────────
with Session(engine) as s:
    s.add(App(user_id=1, name="Api", slug="api", app_dir=str(WORK / "api"), venv_path="", entrypoint="", domain="api.prev.example.com"))
    s.add(Route(app_id=1, hostname="docs.prev.example.com"))
    s.commit()
    if hasattr(previews, "free_domain"):
        d1 = previews.free_domain(s, "api", "prev.example.com")
        d2 = previews.free_domain(s, "docs", "prev.example.com")
        d3 = previews.free_domain(s, "feat-x", "prev.example.com")
    else:
        d1 = d2 = d3 = None
check("preview domain: one in use by an app gets another name", d1 and d1 != "api.prev.example.com"
      and d1.endswith(".prev.example.com"), str(d1))
check("preview domain: one in use by a route too", d2 and d2 != "docs.prev.example.com", str(d2))
check("preview domain: a free one is kept", d3 == "feat-x.prev.example.com", str(d3))

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
