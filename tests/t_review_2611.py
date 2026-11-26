"""Findings of docs/review-2026-11-26.md that need a running server: routes and
port ownership, deploy health without a health check, app-backup restores,
monitors and volumes for restricted operators."""
import io, json, socket, sys, tarfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from harness import admin, free_port, login_as, make_app, server

repo = sys.argv[1]
PORT = 55041
fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "")
    if not cond:
        fails.append(name)


class Private(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"PRIVATE-LOCALHOST-SERVICE")

    def log_message(self, *a):
        pass


def web_app(status):
    return f'''import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response({status}); self.send_header("content-length", "2"); self.end_headers(); self.wfile.write(b"hi")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


def deploy(cl, app_id, timeout=90):
    r = cl.req("POST", f"/api/apps/{app_id}/deploy", json={})
    assert r.status_code == 200, r.text
    end = time.time() + timeout
    while time.time() < end:
        st = cl.req("GET", f"/api/apps/{app_id}/deploy").json()["state"]
        if not st.get("running"):
            return st
        time.sleep(0.5)
    raise TimeoutError("deploy")


fp = free_port()
private = HTTPServer(("127.0.0.1", fp), Private)
threading.Thread(target=private.serve_forever, daemon=True).start()

with server(repo, PORT, env={"VENVY_HEALTH_WAIT_SEC": "20"}) as (base, data, srv):
    cl = admin(base)

    # ── 1. a route can't point at a port that isn't the app's ───────────────
    a = make_app(cl, name="Router", code="import time\nwhile True: time.sleep(0.5)\n")
    cl.req("POST", f"/api/apps/{a}/control?action=start")
    cl.wait_status(a, "running")
    r = cl.req("PUT", f"/api/apps/{a}", json={"manual_ports": [fp]})
    check("(an app's port may still be set to anything without a hostname)", r.status_code == 200, r.text)
    r = cl.req("POST", f"/api/apps/{a}/routes", json={"hostname": "steal.example.test"})
    check("a route can't point at a port that isn't the app's", r.status_code == 409, f"{r.status_code} {r.text}")
    got = httpx.get(base + "/", headers={"host": "steal.example.test"}).text
    check("... and the private service isn't served through the proxy", "PRIVATE-LOCALHOST-SERVICE" not in got, got[:80])
    own = free_port()
    r = cl.req("PUT", f"/api/apps/{a}", json={"manual_ports": [own]})
    r = cl.req("POST", f"/api/apps/{a}/routes", json={"hostname": "ok.example.test"})
    check("a route on the app's own (free) port is fine", r.status_code == 200, f"{r.status_code} {r.text}")
    r = cl.req("PUT", f"/api/apps/{a}", json={"manual_ports": [fp]})
    check("moving an app with a route onto someone else's port is refused", r.status_code == 409, f"{r.status_code} {r.text}")
    r = cl.req("POST", f"/api/apps/{a}/routes", json={"hostname": "go.example.test", "kind": "redirect",
                                                       "redirect_to": "https://example.com"})
    check("a redirect route needs no port check", r.status_code == 200, f"{r.status_code} {r.text}")

    # ── 2. a deploy counts once the app answers ─────────────────────────────
    bad = make_app(cl, name="Gateway", code=web_app(502))
    cl.req("POST", f"/api/apps/{bad}/control?action=start")
    cl.wait_status(bad, "running")
    st = deploy(cl, bad)
    log = "\n".join(st.get("log") or [])
    check("a deploy whose app answers 502 is not ok (rolled back)", st["status"] == "rolled-back", f"{st['status']}\n{log[-600:]}")
    check("... and the log says what it waited for", "waiting for port" in log and "HTTP 502" in log, log[-600:])
    good = make_app(cl, name="Answers", code=web_app(404))
    cl.req("POST", f"/api/apps/{good}/control?action=start")
    cl.wait_status(good, "running")
    st = deploy(cl, good)
    check("a deploy whose app answers (even 404) is ok", st["status"] == "ok", "\n".join(st.get("log") or [])[-600:])
    unchecked = make_app(cl, name="Unchecked", code=web_app(502), health_type="none")
    cl.req("POST", f"/api/apps/{unchecked}/control?action=start")
    cl.wait_status(unchecked, "running")
    st = deploy(cl, unchecked)
    check("an app whose health check is set to None only has to keep running", st["status"] == "ok",
          "\n".join(st.get("log") or [])[-600:])
    worker = make_app(cl, name="Worker", code="import time\nwhile True: time.sleep(0.5)\n")
    cl.req("POST", f"/api/apps/{worker}/control?action=start")
    cl.wait_status(worker, "running")
    st = deploy(cl, worker)
    check("a worker that listens nowhere still deploys", st["status"] == "ok", "\n".join(st.get("log") or [])[-600:])

    # ── 13. the build command saved with the app's settings is kept ─────────
    r = cl.req("PUT", f"/api/apps/{worker}", json={"build_command": "echo built > built.txt"})
    got = cl.req("GET", f"/api/apps/{worker}/deploy").json()["git"].get("build_command")
    check("PUT /apps keeps a build command (Hosting → Run and build)", r.status_code == 200 and got == "echo built > built.txt",
          f"{r.status_code} {got!r}")
    r = cl.req("PUT", f"/api/apps/{worker}", json={"build_command": ""})
    got = cl.req("GET", f"/api/apps/{worker}/deploy").json()["git"].get("build_command")
    check("... and clears it with an empty one", r.status_code == 200 and not got, f"{r.status_code} {got!r}")

    # ── 7. an operator can't turn an app into a compose app by restoring ────
    cl.req("POST", "/api/users", json={"username": "op1", "password": "hunter2hunter2", "role": "operator"})
    op_id = next(u["id"] for u in cl.req("GET", "/api/users").json() if u["username"] == "op1")
    b = make_app(cl, name="Restorable")
    r = cl.req("PUT", f"/api/users/{op_id}/access", json={"all_apps": False, "app_ids": [a, b]})
    check("(operator limited to two apps)", r.status_code == 200, r.text)
    r = cl.req("PUT", f"/api/apps/{b}", json={"runtime": "compose"})
    check("(admin makes it a compose app)", r.status_code == 200, r.text)
    made = cl.job(cl.req("POST", f"/api/apps/{b}/backups"))
    r = cl.req("PUT", f"/api/apps/{b}", json={"runtime": "python", "entrypoint": "main.py"})
    check("(and back to python)", r.status_code == 200, r.text)
    op = login_as(base, "op1", "hunter2hunter2")
    r = op.req("POST", f"/api/apps/{b}/backups/{made['name']}/restore", json={"settings": True})
    check("an operator's settings restore can't bring compose back", r.status_code == 403, f"{r.status_code} {r.text}")
    check("... the app is still python", cl.status(b)["runtime"] == "python")
    res = op.job(op.req("POST", f"/api/apps/{b}/backups/{made['name']}/restore", json={"settings": False}))
    check("... restoring only its files is fine", res.get("ok") is True, str(res))

    # ── 8. a failed restore leaves the app running ──────────────────────────
    cl.req("POST", f"/api/apps/{b}/control?action=start")
    check("(app running)", cl.wait_status(b, "running"))
    bdir = Path(data) / "backups" / "apps" / str(b)
    bdir.mkdir(parents=True, exist_ok=True)
    broken = bdir / f"app-{b}-20260101-000000-abcdef.tar.gz"
    with tarfile.open(broken, "w:gz") as tar:
        for name, body in (("app.json", json.dumps({"runtime": "python"}).encode()), ("content.tar.gz", b"not a tarball")):
            ti = tarfile.TarInfo(name)
            ti.size = len(body)
            tar.addfile(ti, io.BytesIO(body))
    try:
        cl.job(cl.req("POST", f"/api/apps/{b}/backups/{broken.name}/restore", json={}))
        restored = True
    except RuntimeError:
        restored = False
    check("a corrupt backup isn't restored", not restored)
    check("... and the app runs again afterwards", cl.wait_status(b, "running", timeout=20), cl.status(b)["status"])
    left = [p for p in (Path(data)).rglob("restore") if p.is_dir()]
    check("... with nothing half-unpacked left behind", not left, str(left))

    # ── 11. monitors and volumes for an operator limited to some apps ───────
    m = cl.req("POST", "/api/monitors", json={"name": "Site", "target": "https://example.com/", "public": True}).json()
    r = op.req("PUT", f"/api/monitors/{m['id']}", json={"name": "Mine", "target": "https://example.com/"})
    check("a restricted operator can't change the admin's monitor", r.status_code == 403, f"{r.status_code} {r.text}")
    r = op.req("DELETE", f"/api/monitors/{m['id']}")
    check("... nor delete it", r.status_code == 403, f"{r.status_code} {r.text}")
    r = cl.req("PUT", f"/api/monitors/{m['id']}", json={"name": "Site 2", "target": "https://example.com/"})
    check("an admin still can", r.status_code == 200, f"{r.status_code} {r.text}")
    hidden = make_app(cl, name="Hidden")
    v = cl.req("POST", "/api/volumes", json={"name": "shared"}).json()
    cl.req("POST", f"/api/apps/{hidden}/volumes/{v['id']}", json={})
    adm_view = next(x for x in cl.req("GET", "/api/volumes").json() if x["name"] == "shared")
    op_view = next(x for x in op.req("GET", "/api/volumes").json() if x["name"] == "shared")
    check("(the admin sees the volume's app)", [x["app_id"] for x in adm_view["apps"]] == [hidden], str(adm_view))
    check("the volume list hides apps a restricted operator can't see", op_view["apps"] == [], str(op_view))

private.shutdown()
print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
