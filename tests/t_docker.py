"""A Compose app on a real Docker daemon (CI's `docker` job; needs docker and
the compose plugin, and a user that may use them). The rest of the suite uses
a fake `docker`; this is what the fake stands for:

- a Compose app is deployed from an artifact and answers on its port, its
  route and with the app's limits on its container;
- Venvy restarts, with the app's containers already running again (as when
  Docker's restart policy brings them back at boot): the app keeps its port
  and its route;
- a deploy of a version that answers 502 is rolled back, and the site answers
  with the old version again.

    python tests/t_docker.py /path/to/repo
"""
import io, json, shutil, subprocess, sys, tarfile, tempfile, time
from pathlib import Path

import httpx

from harness import admin, server

repo = sys.argv[1]
PORT = 55051
fails = []
ENV = {"VENVY_HEALTH_WAIT_SEC": "45"}


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "", flush=True)
    if not cond:
        fails.append(name)


SERVER = '''import os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
STATUS, BODY = int(sys.argv[1]), sys.argv[2].encode()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(STATUS); self.send_header("content-length", str(len(BODY))); self.end_headers()
        self.wfile.write(BODY)
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
'''


def compose(status, body):
    return f'''services:
  web:
    image: python:3.12-alpine
    command: ["python", "/site/server.py", "{status}", "{body}"]
    ports:
      - "127.0.0.1:${{PORT}}:8000"
    volumes:
      - ./site:/site:ro
    restart: always
'''


def artifact(status, body):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in (("compose.yaml", compose(status, body)), ("site/server.py", SERVER)):
            data = text.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mode = 0o644
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def deploy(cl, app_id, status, body, timeout=300):
    r = cl.req("POST", f"/api/apps/{app_id}/deploy/artifact?start=true",
               files={"file": ("artifact.tar.gz", artifact(status, body), "application/gzip")})
    assert r.status_code == 200, r.text
    end = time.time() + timeout
    time.sleep(1)
    while time.time() < end:
        st = cl.req("GET", f"/api/apps/{app_id}/deploy").json()["state"]
        if not st.get("running"):
            return st
        time.sleep(1)
    raise TimeoutError("deploy")


def get(url, host=None):
    try:
        r = httpx.get(url, headers={"host": host} if host else None, timeout=5)
        return r.status_code, r.text
    except httpx.HTTPError as exc:
        return None, str(exc)


def wait_answer(url, want, host=None, timeout=90):
    end = time.time() + timeout
    got = None
    while time.time() < end:
        got = get(url, host)
        if got[1] == want:
            return True, got
        time.sleep(1)
    return False, got


def docker(*argv):
    return subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=300)


data = Path(tempfile.mkdtemp(prefix="venvy-docker-"))
app_id = None
try:
    with server(repo, PORT, env=ENV, data=data, keep=True) as (base, d, srv):
        cl = admin(base)
        r = cl.req("POST", "/api/apps/upload", data={"name": "Compose", "runtime": "compose", "entrypoint": "compose.yaml",
                                                      "restart_policy": "always", "start_on_boot": "true"})
        assert r.status_code == 200, r.text
        app_id = r.json()["app_id"]
        cl.job(r)
        r = cl.req("PUT", f"/api/apps/{app_id}", json={"memory_limit_mb": 128})
        check("(memory limit set)", r.status_code == 200, r.text)

        st = deploy(cl, app_id, 200, "V1")
        log = "\n".join(st.get("log") or [])
        check("a compose app deploys from an artifact", st["status"] == "ok", log[-1500:])
        if st["status"] != "ok":
            raise SystemExit("FAILED " + str(fails))
        app = cl.status(app_id)
        port = app["port"]
        check("... and is running on a port", app["status"] == "running" and port, str(app)[:300])
        ok, got = wait_answer(f"http://127.0.0.1:{port}/", "V1")
        check("... and answers on it", ok, str(got))
        check("deploy waited for the app's port to answer", f"waiting for port {port}" in log, log[-800:])

        r = cl.req("POST", f"/api/apps/{app_id}/routes", json={"hostname": "compose.example.test"})
        check("a route to a compose app's port (Docker listens on it, not the app's process)", r.status_code == 200, r.text)
        ok, got = wait_answer(base + "/", "V1", host="compose.example.test")
        check("... answers through Venvy's proxy", ok, str(got))
        r = cl.req("GET", f"/api/apps/{app_id}/tunnel/check", params={"hostname": "c.example.test", "local_port": port})
        check("the tunnel check takes the container's port for the app's", r.status_code == 200 and not r.json().get("blocked"),
              f"{r.status_code} {r.text[:300]}")

        ids = docker("ps", "-q", "--filter", f"label=com.docker.compose.project=venvy-app-{app_id}").stdout.split()
        mem = [json.loads(docker("inspect", i).stdout)[0]["HostConfig"]["Memory"] for i in ids]
        check("the app's memory limit is on its container", mem == [128 * 1024 * 1024], str(mem))
        ports_seen = []
        for _ in range(20):
            ports_seen = json.loads(cl.status(app_id).get("detected_ports") or "[]")
            if port in ports_seen:
                break
            time.sleep(1)
        check("the supervisor records the container's port as the app's", port in ports_seen, str(ports_seen))
        sys_ports = cl.req("GET", "/api/system/ports").json()["listening"]
        check("System → Ports in use names the app for it",
              any(p["port"] == port and p.get("app_name") == "Compose" for p in sys_ports), str(sys_ports)[:400])

    # Venvy is down. Docker brings the app's containers back by itself, as
    # their restart policy does at boot, before Venvy starts again.
    rendered = data / ".compose" / f"venvy-app-{app_id}.json"
    check("(the checked compose copy is kept)", rendered.is_file())
    up = docker("compose", "-p", f"venvy-app-{app_id}", "-f", str(rendered), "up", "-d")
    check("(the app's containers run without Venvy)", up.returncode == 0, up.stderr[-500:])

    with server(repo, PORT, env=ENV, data=data, keep=True) as (base, d, srv):
        cl = admin(base)
        check("after a restart of Venvy the app runs again", cl.wait_status(app_id, "running", timeout=180),
              str(cl.status(app_id))[:300])
        app = cl.status(app_id)
        check("... on the same port", app["port"] == port, f"{app['port']} != {port}")
        ok, got = wait_answer(base + "/", "V1", host="compose.example.test")
        check("... and its route still answers", ok, str(got))

        st = deploy(cl, app_id, 502, "BROKEN")
        log = "\n".join(st.get("log") or [])
        check("a deploy that answers 502 is rolled back", st["status"] == "rolled-back", log[-1500:])
        ok, got = wait_answer(f"http://127.0.0.1:{port}/", "V1")
        check("... and the old version answers again", ok, str(got))
        ok, got = wait_answer(base + "/", "V1", host="compose.example.test")
        check("... through its route too", ok, str(got))
        check("... on the same port", cl.status(app_id)["port"] == port)

        r = cl.req("DELETE", f"/api/apps/{app_id}")
        check("the app can be deleted", r.status_code == 200, r.text)
        app_id = None
finally:
    if app_id is not None:
        docker("compose", "-p", f"venvy-app-{app_id}", "down", "--remove-orphans")
    left = docker("ps", "-aq", "--filter", "label=com.docker.compose.project").stdout.split()
    if left:
        print("containers left:", left)
    shutil.rmtree(data, ignore_errors=True)

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
