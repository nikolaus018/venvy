"""Shared helpers for targeted regression scripts: boot a server on a temp
data dir, set up an admin, create a small python app."""
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
        return next(a for a in self.req("GET", "/api/apps").json() if a["id"] == app_id)

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
