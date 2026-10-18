"""Shared helpers for targeted regression scripts: boot a server on a temp
data dir, set up an admin, create a small python app."""
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

PY = sys.executable

APP_CODE = '''\
import signal, sys, time
def bye(*_):
    print("SHUTDOWN-MARKER", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
print("STARTUP-MARKER", flush=True)
while True:
    time.sleep(0.2)
'''


class Client:
    def __init__(self, base):
        self.c = httpx.Client(base_url=base, timeout=30)
        self.key = None

    def _h(self, method):
        h = {}
        if self.key:
            h["x-venvy-key"] = self.key
        if method != "GET":
            h["x-csrf-token"] = self.c.cookies.get("csrf_token") or ""
        return h

    def req(self, method, url, **kw):
        return self.c.request(method, url, headers=self._h(method), **kw)

    def job(self, r, timeout=180):
        body = r.json()
        assert r.status_code == 200, (r.status_code, body)
        if "job_id" not in body:
            return body
        end = time.time() + timeout
        while time.time() < end:
            j = self.req("GET", f"/api/jobs/{body['job_id']}").json()
            if j["status"] == "done":
                return j["result"]
            if j["status"] == "error":
                raise RuntimeError(j["error"])
            time.sleep(0.3)
        raise TimeoutError("job")

    def status(self, app_id):
        r = self.req("GET", "/api/apps")
        if r.status_code != 200 or not isinstance(r.json(), list):
            raise RuntimeError(f"GET /api/apps answered {r.status_code}: {r.text[:500]}")
        return next(a for a in r.json() if a["id"] == app_id)

    def wait_status(self, app_id, want, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            if self.status(app_id)["status"] == want:
                return True
            time.sleep(0.2)
        return False


@contextmanager
def server(repo, port, entry="main.py", env=None, data=None, keep=False):
    data = Path(data or tempfile.mkdtemp(prefix="venvy-t-"))
    log = open(data / "server.log", "a")
    e = {**os.environ, "PYTHONUNBUFFERED": "1", **(env or {})}
    srv = subprocess.Popen([PY, str(Path(repo) / entry), "--port", str(port)], cwd=data,
                           stdout=log, stderr=subprocess.STDOUT, env=e)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(240):
            try:
                if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                pass
            if srv.poll() is not None:
                raise RuntimeError("server died:\n" + (data / "server.log").read_text()[-3000:])
            time.sleep(0.5)
        yield base, data, srv
    except BaseException:
        # The server's side of a crashed script; the data dir is about to go.
        with contextlib.suppress(OSError):
            print("--- server.log (last 6000 chars) ---\n" + (data / "server.log").read_text(errors="replace")[-6000:])
        raise
    finally:
        srv.send_signal(signal.SIGINT)
        try:
            srv.wait(15)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        if not keep:
            shutil.rmtree(data, ignore_errors=True)


def admin(base):
    cl = Client(base)
    cl.c.get("/")
    r = cl.req("POST", "/api/auth/setup", json={"username": "admin", "password": "hunter2hunter2"})
    if r.status_code != 200:
        r = cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"})
    cl.key = r.json()["auth_key"]
    return cl


def make_app(cl, name="T App", code=APP_CODE, **settings):
    r = cl.req("POST", "/api/apps/upload", data={"name": name, "runtime": "python", "entrypoint": "main.py"})
    app_id = r.json()["app_id"]
    cl.job(r)
    r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "main.py", "content": code})
    assert r.status_code == 200, r.text
    if settings:
        r = cl.req("PUT", f"/api/apps/{app_id}", json=settings)
        assert r.status_code == 200, r.text
    return app_id


def app_procs(data, app_id=None):
    """PIDs of python processes running an app entrypoint under this data dir."""
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cwd = p.info["cwd"] or ""
            cmd = p.info["cmdline"] or []
            if str(Path(data).resolve() / "apps") in os.path.realpath(cwd) and cmd and cmd[-1] == "main.py":
                out.append(p.info["pid"])
        except Exception:
            pass
    return out


# ── helpers for the operations tests ────────────────────────────────────────
import json as _json
import socket as _socket
import threading as _threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def free_port():
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Sink:
    """A local HTTP endpoint that records what is sent to it (webhooks, WebDAV, S3)."""

    def __init__(self, status=200):
        self.got = []            # [{"method", "path", "headers", "body"}]
        self.status = status
        sink = self

        class H(BaseHTTPRequestHandler):
            def _take(self):
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n) if n else b""
                sink.got.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                self.send_response(sink.status)
                self.send_header("content-length", "0")
                self.end_headers()
            do_POST = do_PUT = do_GET = _take

            def log_message(self, *a):
                pass

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.httpd.daemon_threads = True
        _threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"

    def json(self):
        return [_json.loads(g["body"]) for g in self.got if g["body"][:1] == b"{"]

    def wait(self, pred, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            hit = [g for g in self.got if pred(g)]
            if hit:
                return hit
            time.sleep(0.3)
        return []

    def close(self):
        self.httpd.shutdown()


SERVER_APP = '''\
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health" and os.path.exists("sick"):
            self.send_response(500); self.end_headers(); self.wfile.write(b"sick"); return
        self.send_response(200); self.send_header("content-type", "text/plain"); self.end_headers()
        self.wfile.write(("APP-OK " + os.environ.get("MARK", "") + " " + self.path).encode())
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        self.send_response(201); self.end_headers(); self.wfile.write(b"POST:" + body)
    def log_message(self, *a): print("REQ", self.path, flush=True)
print("SERVER-START", flush=True)
HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
'''


WS_APP = (Path(__file__).with_name("ws_echo_app.py")).read_text()


def login_as(base, username, password):
    cl = Client(base)
    cl.c.get("/")
    r = cl.req("POST", "/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    cl.key = r.json()["auth_key"]
    return cl
