"""Hosting anything, end to end, through the API of a real server:
any command / binary, root folders, runtime detection, workers and replicas
behind the proxy, zero-downtime restarts, routes (aliases, paths, wildcards,
redirects, basic auth, IP allow-lists, rate limits, body limits, headers,
gzip, cache), access logs and /metrics, TCP / command health checks, stop
signals, one-off commands, host jobs, secret files and $APP_DATA_DIR,
monitors and the status page, TCP and UDP port forwards, volumes, log
shipping and Vault."""
import gzip, json, os, socket, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx
from harness import Sink, admin, free_port, login_as, server

repo = sys.argv[1]
fails = []


def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c:
        fails.append(n)


ECHO = '''\
import os, signal, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
def bye(sig, _):
    print("GOT-" + signal.Signals(sig).name, flush=True); sys.exit(0)
signal.signal(signal.SIGINT, bye); signal.signal(signal.SIGTERM, bye)
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send(self, code, body, ctype="text/plain", extra=()):
        self.send_response(code); self.send_header("content-type", ctype)
        for k, v in extra: self.send_header(k, v)
        self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path == "/secret":
            return self._send(200, open(os.path.join(os.environ["SECRETS_DIR"], "token.txt"), "rb").read())
        if self.path == "/data":
            return self._send(200, os.environ["APP_DATA_DIR"].encode())
        if self.path == "/env":
            return self._send(200, os.environ.get(self.headers.get("x-var", ""), "-").encode())
        if self.path == "/big":
            return self._send(200, b"x" * 5000, "text/html")
        if self.path == "/cached":
            return self._send(200, str(time.time()).encode(), extra=[("cache-control", "public, max-age=60")])
        self._send(200, ("PORT=" + os.environ.get("PORT", "") + " PATH=" + self.path + " P=" + os.environ.get("VENVY_PROCESS", "web")
                         + " PREFIX=" + self.headers.get("x-forwarded-prefix", "")).encode())
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); b = self.rfile.read(n); self._send(200, b"GOT " + str(len(b)).encode())
    def log_message(self, *a): pass
print("ECHO-UP", os.environ.get("PORT"), flush=True)
ThreadingHTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''

with server(repo, 55026) as (base, data, srv):  # its own port: t_ops (55021) runs in another lane
    cl = admin(base)
    tok = cl.req("POST", "/api/tokens", json={"name": "metrics", "scope": "read"}).json()["token"]

    def make(name, runtime="command", files=None, **settings):
        form = {"name": name, "runtime": runtime, "entrypoint": settings.pop("entrypoint", ""),
                "start_command": settings.pop("start_command", ""), "root_dir": settings.pop("root_dir", "")}
        r = cl.req("POST", "/api/apps/upload", data=form)
        assert r.status_code == 200, r.text
        aid = r.json()["app_id"]
        cl.job(r)
        for path, content in (files or {}).items():
            rr = cl.req("POST", f"/api/apps/{aid}/files/op", json={"operation": "save", "path": path, "content": content})
            assert rr.status_code == 200, rr.text
        if settings:
            rr = cl.req("PUT", f"/api/apps/{aid}", json=settings)
            assert rr.status_code == 200, rr.text
        return aid

    def start(aid, timeout=40):
        cl.req("POST", f"/api/apps/{aid}/control?action=start")
        end = time.time() + timeout
        while time.time() < end:
            a = cl.status(aid)
            if a["status"] in ("running", "error"):
                if a["status"] == "error" or a["port"]:
                    return a
            time.sleep(0.3)
        return cl.status(aid)

    def logs(aid):
        return cl.req("GET", f"/api/apps/{aid}/logs").json()["content"]

    def get(port, path="/", **kw):
        for _ in range(60):
            try:
                return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=3, **kw)
            except Exception:
                time.sleep(0.3)

    def via(host, path="/", method="GET", **kw):
        return httpx.request(method, base + path, headers={"host": host, **kw.pop("headers", {})}, timeout=10, **kw)

    def wait_for(fn, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = fn()
                if v:
                    return v
            except Exception:
                pass
            time.sleep(0.3)
        return None

    # ── any command, $PORT, root folder ──────────────────────────────────
    p1 = free_port()
    a1 = make("Echo", files={"svc/echo.py": ECHO}, start_command=f"{sys.executable} echo.py --x $PORT",
              root_dir="svc", manual_ports=[p1], stop_signal="SIGINT")
    a = start(a1)
    r = get(p1)
    check("command runtime runs the start command in the root folder", a["status"] == "running" and r is not None
          and r.text.startswith(f"PORT={p1}"), logs(a1)[-400:])
    check("$PORT expanded in the start command", f"--x {p1}" in " ".join(__import__("psutil").Process(a["pid"]).cmdline()) if a.get("pid") else True)
    check("APP_DATA_DIR exists outside the app folder", wait_for(lambda: (lambda t: os.path.isdir(t) and "app-state" in t)(get(p1, "/data").text)))
    cl.req("POST", f"/api/apps/{a1}/control?action=stop")
    cl.wait_status(a1, "stopped")
    check("stop signal SIGINT reaches the app", "GOT-SIGINT" in logs(a1), logs(a1)[-300:])
    check("bad root folder refused", cl.req("PUT", f"/api/apps/{a1}", json={"root_dir": "../x"}).status_code == 400)
    check("bad stop signal refused", cl.req("PUT", f"/api/apps/{a1}", json={"stop_signal": "SIGKILL"}).status_code == 400)

    # ── auto-detection on create ─────────────────────────────────────────
    r = cl.req("POST", "/api/apps/upload", data={"name": "Auto", "runtime": "auto"},
               files=[("files", ("index.html", b"<h1>hi</h1>"))])
    check("runtime auto: a static site is recognised", r.status_code == 200, r.text)
    if r.status_code == 200:
        cl.job(r)
        check("... and stored as static", cl.status(r.json()["app_id"])["runtime"] == "static")
    r = cl.req("POST", "/api/apps/upload", data={"name": "Unknown", "runtime": "auto"},
               files=[("files", ("README", b"just text"))])
    check("runtime auto: nothing recognisable -> 400", r.status_code == 400, r.text)

    # ── secret files ─────────────────────────────────────────────────────
    check("secret files saved", cl.req("PUT", f"/api/apps/{a1}", json={"secret_files": {"token.txt": "TOP-SECRET"}}).status_code == 200)
    check("secret file names are checked", cl.req("PUT", f"/api/apps/{a1}", json={"secret_files": {"../x": "y"}}).status_code == 400)
    check("secret files never returned by the API", "TOP-SECRET" not in json.dumps(cl.status(a1)) and cl.status(a1)["has_secret_files"])
    start(a1)
    check("secret file is in $SECRETS_DIR at start", wait_for(lambda: get(p1, "/secret").text == "TOP-SECRET"))

    # ── health checks: tcp and command ───────────────────────────────────
    cl.req("PUT", f"/api/apps/{a1}", json={"health_type": "tcp", "health_interval": 5})
    check("tcp health check passes", wait_for(lambda: (cl.status(a1).get("health") or {}).get("status") == "healthy", 40),
          str(cl.status(a1).get("health")))
    cl.req("PUT", f"/api/apps/{a1}", json={"health_type": "command", "health_command": "test -f ok.flag",
                                           "health_failures": 1, "health_restart": False})
    check("command health check fails without the file", wait_for(lambda: (cl.status(a1).get("health") or {}).get("status") == "unhealthy", 40),
          str(cl.status(a1).get("health")))
    cl.req("POST", f"/api/apps/{a1}/files/op", json={"operation": "save", "path": "svc/ok.flag", "content": "1"})
    check("... and passes with it (runs in the root folder)", wait_for(lambda: (cl.status(a1).get("health") or {}).get("status") == "healthy", 40))
    cl.req("PUT", f"/api/apps/{a1}", json={"health_type": "none"})

    # ── one-off command ──────────────────────────────────────────────────
    r = cl.req("POST", f"/api/apps/{a1}/run", json={"command": "echo hello-$VENVY_APP_ID && pwd"})
    res = cl.job(r)
    check("one-off command runs as the app, in its folder", res["status"] == "ok" and f"hello-{a1}" in res["output"]
          and res["output"].strip().endswith("svc"), str(res))

    # ── host jobs ────────────────────────────────────────────────────────
    r = cl.req("POST", "/api/system/jobs", json={"name": "ping", "schedule": "0 0 * * *", "command": "echo HOST-JOB"})
    check("host job created", r.status_code == 200, r.text)
    jid = r.json().get("id")
    cl.req("POST", f"/api/system/jobs/{jid}/run")
    check("host job runs", wait_for(lambda: "HOST-JOB" in next(j for j in cl.req("GET", "/api/system/jobs").json() if j["id"] == jid)["last_output"]))

    # ── workers and replicas behind the proxy ────────────────────────────
    r = cl.req("POST", f"/api/apps/{a1}/processes", json={"name": "worker", "command": f"{sys.executable} -c \"import time,os; print('WORKER', os.environ['VENVY_PROCESS'], flush=True); time.sleep(600)\""})
    check("worker added", r.status_code == 200 and any(p["process_kind"] == "worker" for p in r.json()), r.text)
    check("worker name 'web' refused", cl.req("POST", f"/api/apps/{a1}/processes", json={"name": "web", "command": "x"}).status_code == 400)
    rr = cl.req("POST", f"/api/apps/{a1}/routes", json={"hostname": "echo.test"})
    check("hostname routed to the app", rr.status_code == 200, rr.text)
    rr = cl.req("PUT", f"/api/apps/{a1}", json={"instances": 2})
    check("instances set", rr.status_code == 200, rr.text)
    procs = cl.req("GET", f"/api/apps/{a1}/processes").json()
    check("instances=2 made a replica", sum(p["process_kind"] == "replica" for p in procs) == 1, str([p["process_kind"] for p in procs]))
    cl.req("POST", f"/api/apps/{a1}/control?action=restart")
    time.sleep(1)
    cl.req("POST", f"/api/apps/{a1}/control?action=start")
    check("group start runs every process", wait_for(lambda: all(p["status"] == "running" for p in cl.req("GET", f"/api/apps/{a1}/processes").json()), 40),
          str([(p["process_kind"], p["status"]) for p in cl.req("GET", f"/api/apps/{a1}/processes").json()]))
    worker = next(p for p in cl.req("GET", f"/api/apps/{a1}/processes").json() if p["process_kind"] == "worker")
    check("worker has its own log and its name", wait_for(lambda: "WORKER worker" in logs(worker["id"])), logs(worker["id"])[-200:])
    seen = set()
    for _ in range(12):
        rr = via("echo.test")
        if rr.status_code == 200:
            seen.add(rr.text.split()[0])
    check("proxy spreads requests over the app and its replica", len(seen) == 2, str(seen))
    # zero-downtime restart: no request fails while it runs
    errors = []
    stop_flag = threading.Event()

    def hammer():
        while not stop_flag.is_set():
            try:
                rr_ = via("echo.test")
                if rr_.status_code != 200:
                    errors.append(f"status {rr_.status_code} {rr_.text[:60]}")
            except Exception as exc:
                errors.append(str(exc))
            time.sleep(0.05)
    t = threading.Thread(target=hammer)
    t.start()
    cl.req("POST", f"/api/apps/{a1}/control?action=restart")
    time.sleep(1)
    wait_for(lambda: all(p["status"] == "running" for p in cl.req("GET", f"/api/apps/{a1}/processes").json())
             and cl.status(a1)["status"] == "running", 60)
    time.sleep(2)
    stop_flag.set()
    t.join()
    check("rolling restart over replicas drops no request", not errors, str(errors[:5]))
    cl.req("PUT", f"/api/apps/{a1}", json={"instances": 1, "zero_downtime": True})
    check("instances=1 removed the replica", not any(p["process_kind"] == "replica" for p in cl.req("GET", f"/api/apps/{a1}/processes").json()))
    errors.clear()
    stop_flag.clear()
    t = threading.Thread(target=hammer)
    t.start()
    cl.req("POST", f"/api/apps/{a1}/control?action=restart")
    time.sleep(1)
    wait_for(lambda: cl.status(a1)["status"] == "running" and not any(p["process_name"] == "surge" for p in cl.req("GET", f"/api/apps/{a1}/processes").json()), 60)
    time.sleep(1)
    stop_flag.set()
    t.join()
    check("zero-downtime restart of a single instance drops no request", not errors, str(errors[:5]))

    # ── routes ───────────────────────────────────────────────────────────
    p2 = free_port()
    a2 = make("Api", files={"echo.py": ECHO}, start_command=f"{sys.executable} echo.py", manual_ports=[p2])
    start(a2)
    get(p2)
    R = lambda body: cl.req("POST", f"/api/apps/{a2}/routes", json=body)
    check("path route with prefix strip", R({"hostname": "echo.test", "path_prefix": "/api", "strip_prefix": True}).status_code == 200)
    rr = via("echo.test", "/api/users")
    check("... /api goes to the other app, prefix stripped", rr.status_code == 200 and f"PORT={p2}" in rr.text and "PATH=/users" in rr.text
          and "PREFIX=/api" in rr.text, rr.text)
    check("... the rest still goes to the first app", f"PORT={p2}" not in via("echo.test", "/apix").text)

    def raw(host, target):
        # Sent as written: an HTTP client would tidy the path up first.
        with socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])), timeout=10) as sk:
            sk.sendall(f"GET {target} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            data = b""
            while True:
                chunk = sk.recv(65536)
                if not chunk:
                    break
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split()[1]), body.decode(errors="replace")
    code, body = raw("echo.test", "/%61pi/users")
    check("an encoded path still takes its path route", code == 200 and f"PORT={p2}" in body and "PATH=/users" in body, f"{code} {body[:200]}")
    code, body = raw("echo.test", "//api/users")
    check("... and so does a doubled slash", code == 200 and f"PORT={p2}" in body, f"{code} {body[:200]}")
    check("an encoded slash on a path-routed host is refused", raw("echo.test", "/x/..%2Fapi/users")[0] == 400)
    R({"hostname": "echo.test", "path_prefix": "/admin", "basic_auth": {"ann": "pa55word"}})
    check("a path route's basic auth can't be skipped with an encoded path", raw("echo.test", "/%61dmin")[0] == 401
          and raw("echo.test", "/x/../admin")[0] == 401)
    # Someone who manages only this app can't take another app's hostname.
    cl.req("POST", "/api/users", json={"username": "op1", "password": "hunter2hunter2", "role": "operator"})
    op_id = next(u["id"] for u in cl.req("GET", "/api/users").json() if u["username"] == "op1")
    cl.req("PUT", f"/api/users/{op_id}/access", json={"all_apps": False, "app_ids": [a2]})
    op = login_as(base, "op1", "hunter2hunter2")
    rr = op.req("POST", f"/api/apps/{a2}/routes", json={"hostname": "echo.test", "path_prefix": "/login"})
    check("an operator can't route a path of another app's hostname", rr.status_code == 409, rr.text)
    rr = op.req("PUT", f"/api/apps/{a2}", json={"domain": "echo.test"})
    check("... nor take that hostname as the app's domain", rr.status_code == 409, rr.text)
    rr = op.req("POST", "/api/apps/upload", data={"name": "Compose", "runtime": "compose"})
    check("only an admin makes compose apps", rr.status_code == 403, rr.text)
    rr = op.req("PUT", f"/api/apps/{a2}", json={"runtime": "compose"})
    check("... or switches an app to compose", rr.status_code == 403, rr.text)
    check("alias hostname", R({"hostname": "alias.test"}).status_code == 200 and f"PORT={p2}" in via("alias.test").text)
    check("wildcard hostname", R({"hostname": "*.wild.test"}).status_code == 200 and f"PORT={p2}" in via("abc.wild.test").text)
    check("duplicate route refused", R({"hostname": "alias.test"}).status_code == 409)
    check("Venvy's own name refused", R({"hostname": "127.0.0.1"}).status_code == 400)
    R({"hostname": "www.alias.test", "kind": "redirect", "redirect_to": "alias.test", "redirect_code": 308})
    rr = via("www.alias.test", "/x?y=1")
    check("redirect route", rr.status_code == 308 and rr.headers["location"] == "http://alias.test/x?y=1", f"{rr.status_code} {rr.headers}")
    R({"hostname": "locked.test", "basic_auth": {"ann": "pa55word"}, "headers": {"X-Frame-Options": "DENY"}})
    check("basic auth: 401 without", via("locked.test").status_code == 401)
    rr = via("locked.test", auth=("ann", "pa55word"))
    check("basic auth: 200 with, extra header added", rr.status_code == 200 and rr.headers.get("x-frame-options") == "DENY", str(rr.headers))
    check("basic auth: wrong password", via("locked.test", auth=("ann", "nope")).status_code == 401)
    R({"hostname": "ip.test", "ip_allow": "10.0.0.0/8"})
    check("IP allow-list refuses others", via("ip.test").status_code == 403)
    R({"hostname": "rate.test", "rate_limit": 3})
    codes = [via("rate.test").status_code for _ in range(5)]
    check("rate limit", codes[:3] == [200] * 3 and codes[3] == 429, str(codes))
    R({"hostname": "body.test", "max_body_mb": 1})
    check("body limit", via("body.test", "/", "POST", content=b"x" * (2 * 1024 * 1024)).status_code == 413
          and via("body.test", "/", "POST", content=b"x" * 100).text == "GOT 100")
    R({"hostname": "gz.test", "gzip": True, "cache_seconds": 30})
    rr = httpx.get(base + "/big", headers={"host": "gz.test", "accept-encoding": "gzip"})
    check("gzip", rr.headers.get("content-encoding") == "gzip" and rr.text == "x" * 5000, str(rr.headers))
    c1 = via("gz.test", "/cached")
    c2 = via("gz.test", "/cached")
    check("cache: second answer from the cache", c1.text == c2.text and c2.headers.get("x-venvy-cache") == "HIT", str(c2.headers))
    check("routes listed without passwords", all("pa55word" not in json.dumps(x) for x in cl.req("GET", f"/api/apps/{a2}/routes").json()))
    time.sleep(3)
    al = cl.req("GET", f"/api/apps/{a2}/access-log").json()["lines"]
    check("access log written", any("/users" in ln and " 200 " in ln for ln in al), str(al[-3:]))
    check("traffic counted", cl.req("GET", f"/api/apps/{a2}/traffic").json()["requests"].get("2xx", 0) > 0)
    m = httpx.get(base + "/metrics", headers={"authorization": f"Bearer {tok}"})
    check("/metrics with a read token", m.status_code == 200 and "venvy_http_requests_total" in m.text and 'app="Api"' in m.text, m.text[:200])
    check("/metrics needs auth", httpx.get(base + "/metrics").status_code == 401)

    # ── port forwards ────────────────────────────────────────────────────
    fp = free_port()
    r = cl.req("POST", f"/api/apps/{a2}/forwards", json={"protocol": "tcp", "listen_port": fp, "listen_host": "127.0.0.1"})
    check("tcp forward created", r.status_code == 200, r.text)
    rr = wait_for(lambda: httpx.get(f"http://127.0.0.1:{fp}/via-forward", timeout=3))
    check("tcp forward reaches the app", rr is not None and "PATH=/via-forward" in rr.text)
    check("same port twice refused", cl.req("POST", f"/api/apps/{a2}/forwards", json={"listen_port": fp}).status_code == 409)
    up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    up.bind(("127.0.0.1", 0))
    udp_target = up.getsockname()[1]

    def udp_echo():
        up.settimeout(20)
        try:
            d, addr = up.recvfrom(100)
            up.sendto(b"ECHO:" + d, addr)
        except OSError:
            pass
    threading.Thread(target=udp_echo, daemon=True).start()
    ufp = free_port()
    cl.req("POST", f"/api/apps/{a2}/forwards", json={"protocol": "udp", "listen_port": ufp, "target_port": udp_target, "listen_host": "127.0.0.1"})
    time.sleep(1)
    c = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    c.settimeout(5)
    got = b""
    for _ in range(5):
        try:
            c.sendto(b"hi", ("127.0.0.1", ufp))
            got = c.recvfrom(100)[0]
            break
        except OSError:
            time.sleep(0.5)
    check("udp forward relays both ways", got == b"ECHO:hi", repr(got))

    # ── volumes ──────────────────────────────────────────────────────────
    v = cl.req("POST", "/api/volumes", json={"name": "shared"}).json()
    check("volume created", "id" in v, str(v))
    check("bad volume name refused", cl.req("POST", "/api/volumes", json={"name": "Bad Name"}).status_code == 400)
    check("volume attached", cl.req("POST", f"/api/apps/{a2}/volumes/{v['id']}", json={"read_only": False}).status_code == 200)
    cl.req("POST", f"/api/apps/{a2}/control?action=restart")
    time.sleep(1)
    wait_for(lambda: cl.status(a2)["status"] == "running")
    check("$VOLUME_SHARED given to the app", wait_for(lambda: get(p2, "/env", headers={"x-var": "VOLUME_SHARED"}).text.endswith("volumes/shared")))
    check("attached volume can't be deleted", cl.req("DELETE", f"/api/volumes/{v['id']}").status_code == 409)

    # ── monitors, uptime, status page ────────────────────────────────────
    sink = Sink()
    mr = cl.req("POST", "/api/monitors", json={"name": "Sink", "target": sink.url + "/ping", "interval": 10, "public": True})
    check("monitor created", mr.status_code == 200, mr.text)
    check("monitor target validated", cl.req("POST", "/api/monitors", json={"name": "x", "target": "ftp://x"}).status_code == 400)
    check("monitor checks the URL", wait_for(lambda: next(x for x in cl.req("GET", "/api/monitors").json() if x["name"] == "Sink")["last_ok"], 30))
    check("status page off by default", httpx.get(base + "/status").status_code == 404)
    cl.req("PUT", "/api/system/status-page", json={"enabled": True, "title": "Acme status", "apps": [a2]})
    sp = httpx.get(base + "/status")
    check("status page lists the app and the monitor", sp.status_code == 200 and "Acme status" in sp.text and "Api" in sp.text and "Sink" in sp.text)
    check("status.json", httpx.get(base + "/status.json").json()["title"] == "Acme status")

    # ── heartbeat and OTLP push ──────────────────────────────────────────
    cl.req("PUT", "/api/system/telemetry", json={"heartbeat_url": sink.url + "/hb", "otlp_url": sink.url})
    check("heartbeat sent", bool(sink.wait(lambda g: g["path"] == "/hb", 90)))
    check("OTLP metrics pushed", bool(sink.wait(lambda g: g["path"] == "/v1/metrics" and b"resourceMetrics" in g["body"], 90)))

    # ── log shipping ─────────────────────────────────────────────────────
    r = cl.req("PUT", "/api/system/log-targets", json=[{"kind": "http", "url": sink.url + "/logs"},
                                                       {"kind": "syslog", "host": "127.0.0.1", "port": 1, "protocol": "udp"}])
    check("log targets saved", r.status_code == 200 and len(r.json()) == 2, r.text)
    cl.req("POST", f"/api/apps/{a2}/control?action=restart")
    hit = sink.wait(lambda g: g["path"] == "/logs" and b"ECHO-UP" in g["body"], 30)
    check("app log lines shipped as JSON", bool(hit) and json.loads(hit[0]["body"])[0]["app"] == "Api")

    # ── Vault ────────────────────────────────────────────────────────────
    class V(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("x-vault-token") == "vt" and self.path == "/v1/secret/data/api"
            body = json.dumps({"data": {"data": {"FROM_VAULT": "vault-value"}}}).encode() if ok else b"{}"
            self.send_response(200 if ok else 403)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    vs = ThreadingHTTPServer(("127.0.0.1", 0), V)
    threading.Thread(target=vs.serve_forever, daemon=True).start()
    r = cl.req("PUT", "/api/system/secrets", json={"vault_addr": f"http://127.0.0.1:{vs.server_address[1]}", "vault_token": "vt"})
    check("vault configured", r.status_code == 200 and r.json()["vault_token_set"], r.text)
    cl.req("PUT", f"/api/apps/{a2}", json={"vault_path": "secret/api"})
    cl.req("POST", f"/api/apps/{a2}/control?action=restart")
    time.sleep(1)
    wait_for(lambda: cl.status(a2)["status"] == "running")
    check("Vault values reach the app's environment", wait_for(lambda: get(p2, "/env", headers={"x-var": "FROM_VAULT"}).text == "vault-value"))
    cl.req("PUT", f"/api/apps/{a2}", json={"vault_path": "secret/missing"})
    cl.req("POST", f"/api/apps/{a2}/control?action=restart")
    check("a Vault error stops the start with a reason", wait_for(lambda: cl.status(a2)["status"] == "error", 20)
          and "Vault" in logs(a2), logs(a2)[-300:])
    vs.shutdown()
    sink.close()

    # ── deleting an app takes its processes and routes with it ───────────
    cl.req("POST", f"/api/apps/{a1}/control?action=stop")
    r = cl.req("DELETE", f"/api/apps/{a1}")
    check("app with workers deleted", r.status_code == 200, r.text)
    left = [x for x in cl.req("GET", "/api/apps").json() if x.get("parent_app_id") == a1]
    check("... and its workers too", not left, str(left))
    check("... its domain is free again", via("echo.test").status_code != 200 or "PORT=" not in via("echo.test").text)

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
