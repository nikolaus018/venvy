"""End-to-end smoke pass against a real Venvy server in a throwaway data dir.

usage: smoke.py <repo_dir> [--keep]
Exits non-zero on the first failed check.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(sys.argv[1]).resolve()
PORT = int(os.environ.get("SMOKE_PORT", "54991"))
BASE = f"http://127.0.0.1:{PORT}"
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

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


class Client:
    def __init__(self):
        self.c = httpx.Client(base_url=BASE, timeout=30)
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


def wait_status(cl, app_id, want, timeout=20):
    end = time.time() + timeout
    st = None
    while time.time() < end:
        apps = cl.req("GET", "/api/apps").json()
        st = next(a for a in apps if a["id"] == app_id)["status"]
        if st == want:
            return True
        time.sleep(0.2)
    print("     last status:", st)
    return False


def main():
    data = Path(tempfile.mkdtemp(prefix="venvy-smoke-"))
    log = open(data / "server.log", "w")
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    entry = os.environ.get("SMOKE_ENTRY", "main.py")
    srv = subprocess.Popen([PY, str(REPO / entry), "--port", str(PORT)], cwd=data, stdout=log, stderr=subprocess.STDOUT, env=env)
    try:
        for _ in range(240):
            try:
                if httpx.get(BASE + "/api/health", timeout=1).status_code == 200:
                    break
            except Exception:
                pass
            if srv.poll() is not None:
                break
            time.sleep(0.5)
        check("server starts", srv.poll() is None and httpx.get(BASE + "/api/health").json()["ok"])

        cl = Client()
        r = cl.c.get("/")
        check("index served", r.status_code == 200 and "Venvy" in r.text)
        check("built CSS served", cl.c.get("/static/css/tailwind.css").status_code == 200 and ".hidden{display:none}" in cl.c.get("/static/css/tailwind.css").text)
        st = cl.req("GET", "/api/auth/status").json()
        check("fresh instance has no user", st["user_exists"] is False)
        check("setup code file is 0600", (os.stat(data / ".setup-code").st_mode & 0o777) == 0o600)
        r = cl.req("POST", "/api/auth/setup", json={"username": "admin", "password": "hunter2hunter2"})
        check("setup without the code refused", r.status_code == 403, r.text)
        r = cl.req("POST", "/api/auth/setup", json={"username": "admin", "password": "hunter2hunter2", "setup_code": "0000-0000-0000-0000"})
        check("setup with a wrong code refused", r.status_code == 403, r.text)
        code = (data / ".setup-code").read_text().strip()
        r = cl.req("POST", "/api/auth/setup", json={"username": "admin", "password": "hunter2hunter2", "setup_code": code.upper()})
        check("setup", r.status_code == 200, r.text)
        check("setup code removed after setup", not (data / ".setup-code").exists())
        cl.key = r.json()["auth_key"]
        check("authenticated", cl.req("GET", "/api/auth/status").json()["authenticated"])
        check("cookie alone is not enough", httpx.get(BASE + "/api/apps", cookies=dict(cl.c.cookies)).status_code == 401)
        check("csrf enforced", cl.c.post("/api/auth/logout", headers={"x-venvy-key": cl.key}).status_code == 403)

        # login / logout round trip
        r = cl.req("POST", "/api/auth/logout")
        check("logout", r.status_code == 200)
        check("logged out", cl.req("GET", "/api/apps").status_code == 401)
        r = cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "wrong-password1"})
        check("bad password rejected", r.status_code == 401)
        r = cl.req("POST", "/api/auth/login", json={"username": "nobody", "password": "wrong-password1"})
        check("unknown user rejected", r.status_code == 401)
        r = cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"})
        check("login", r.status_code == 200, r.text)
        cl.key = r.json()["auth_key"]

        # users / roles
        r = cl.req("POST", "/api/users", json={"username": "view", "password": "viewer12345", "role": "viewer"})
        check("create viewer", r.status_code == 200, r.text)
        v = Client()
        v.c.get("/")
        r = v.req("POST", "/api/auth/login", json={"username": "view", "password": "viewer12345"})
        v.key = r.json()["auth_key"]
        check("viewer can list apps", v.req("GET", "/api/apps").status_code == 200)
        check("viewer cannot back up", v.req("POST", "/api/system/backup").status_code == 403)
        check("viewer cannot list users", v.req("GET", "/api/users").status_code == 403)

        # create app
        r = cl.req("POST", "/api/apps/upload", data={"name": "Smoke App", "runtime": "python", "entrypoint": "main.py"})
        check("create app accepted", r.status_code == 200, r.text)
        app_id = r.json()["app_id"]
        created = cl.job(r)
        check("app created", created["status"] == "stopped" and created["id"] == app_id)
        check("viewer cannot control", v.req("POST", f"/api/apps/{app_id}/control?action=start").status_code == 403)
        check("viewer sees redacted env", v.req("GET", "/api/apps").json()[0]["env_json"] == "{}")

        # files: save, tree, download, traversal
        r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "main.py", "content": APP_CODE})
        check("save file", r.status_code == 200, r.text)
        tree = cl.req("GET", f"/api/apps/{app_id}/files/tree").json()
        check("tree lists file", any(f["name"] == "main.py" for f in tree))
        r = cl.req("GET", f"/api/apps/{app_id}/files/download", params={"path": "main.py"})
        check("download file", r.status_code == 200 and "STARTUP-MARKER" in r.text)
        r = cl.req("GET", f"/api/apps/{app_id}/files/download", params={"path": "../../data.db"})
        check("traversal blocked (download)", r.status_code == 403)
        r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "../../evil", "content": "x"})
        check("traversal blocked (save)", r.status_code == 403)
        # chunked upload
        payload = b"hello chunk\n" * 10
        r = cl.req("POST", f"/api/apps/{app_id}/files/upload/chunk",
                   data={"upload_id": "ab" * 16, "path": "", "rel_path": "sub/up.txt", "offset": "0", "total": str(len(payload))},
                   files={"chunk": ("chunk", payload)})
        check("chunk upload", r.status_code == 200 and r.json()["done"], r.text)
        r = cl.req("GET", f"/api/apps/{app_id}/files/download", params={"path": "sub/up.txt"})
        check("uploaded content", r.content == payload)
        r = cl.req("POST", "/api/auth/download-ticket")
        t = r.json()["ticket"]
        r = cl.c.get(f"/api/apps/{app_id}/files/download", params={"path": "main.py", "ticket": t})
        check("ticket download", r.status_code == 200)
        r = cl.c.get(f"/api/apps/{app_id}/files/download", params={"path": "main.py", "ticket": t})
        check("ticket is single-use", r.status_code == 401)

        # settings + env encryption at rest
        r = cl.req("PUT", f"/api/apps/{app_id}", json={"env_vars": {"SECRET_THING": "s3cr3t-value"}, "restart_policy": "never"})
        check("update app", r.status_code == 200 and "s3cr3t-value" in r.json()["env_json"], r.text)
        import sqlite3
        con = sqlite3.connect(data / "data.db")
        raw = con.execute("select env_json from app").fetchone()[0]
        con.close()
        check("env encrypted at rest", raw.startswith("enc:v1:") and "s3cr3t" not in raw)
        check("key file is 0600", (os.stat(data / ".venvy_key").st_mode & 0o777) == 0o600)

        # lifecycle
        r = cl.req("POST", f"/api/apps/{app_id}/control?action=start")
        check("start accepted", r.status_code == 200, r.text)
        check("app running", wait_status(cl, app_id, "running"))
        time.sleep(1.0)
        logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
        check("logs captured", "STARTUP-MARKER" in logs, logs[-300:])
        r = cl.req("POST", f"/api/apps/{app_id}/control?action=restart")
        check("restart accepted", r.status_code == 200)
        time.sleep(2.5)
        check("app running after restart", wait_status(cl, app_id, "running"))
        r = cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
        check("stop accepted", r.status_code == 200)
        check("app stopped", wait_status(cl, app_id, "stopped"))
        time.sleep(0.5)
        logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
        if os.environ.get("SMOKE_EXPECT_SHUTDOWN_LOG") == "1":
            check("shutdown output captured", "SHUTDOWN-MARKER" in logs, logs[-300:])
        else:
            print("  info shutdown output captured:", "SHUTDOWN-MARKER" in logs)
        check("packages list", isinstance(cl.req("GET", f"/api/apps/{app_id}/packages").json(), list))

        # system
        check("metrics", cl.req("GET", "/api/system/metrics").status_code == 200)
        check("ports", cl.req("GET", "/api/system/ports").status_code == 200)
        r = cl.req("PUT", "/api/system/settings/cf_api_token", json={"value": "tok-123"})
        check("set setting", r.status_code == 200)
        g = cl.req("GET", "/api/system/settings/cf_api_token").json()
        check("secret setting is write-only", g["value"] == "" and g["is_set"] is True, str(g))
        cl.req("POST", "/api/system/settings/batch", json=[{"key": "cf_account_id", "value": "acc0123456789"}, {"key": "cf_tunnel_id", "value": "tun0123456789"}])
        check("plain cf setting readable", cl.req("GET", "/api/system/settings/cf_account_id").json()["value"] == "acc0123456789")
        stt = cl.req("GET", "/api/system/cloudflare/status").json()
        check("cf status", stt == {"configured": True, "token_set": True, "account_id_hint": "acc01234", "tunnel_id_hint": "tun01234"}, str(stt))
        check("cf status hidden from viewer", v.req("GET", "/api/system/cloudflare/status").status_code == 403)
        raw = sqlite3.connect(data / "data.db").execute("select value from systemsetting where key='cf_api_token'").fetchone()[0]
        check("token encrypted at rest", raw.startswith("enc:v1:"))

        # backup
        b = cl.job(cl.req("POST", "/api/system/backup"))
        check("backup created", b["ok"] and Path(b["path"]).is_file())
        lst = cl.req("GET", "/api/system/backups").json()
        check("backup listed", len(lst) == 1 and lst[0]["type"] == "full")
        import tarfile
        with tarfile.open(b["path"]) as tar:
            names = tar.getnames()
        check("backup has db+key+apps", "data.db" in names and ".venvy_key" in names and "apps" in names)

        # restore (in place) — mutate, restore, expect the mutation gone
        if os.environ.get("SMOKE_RESTORE", "1") == "1":
            r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "after-backup.txt", "content": "x"})
            check("post-backup file", r.status_code == 200)
            r = cl.req("POST", f"/api/system/backup/restore/{lst[0]['filename']}", json={"confirm": "nope"})
            check("restore needs confirm phrase", r.status_code == 400)
            res = cl.job(cl.req("POST", f"/api/system/backup/restore/{lst[0]['filename']}", json={"confirm": "RESTORE"}))
            check("restore job done", res.get("restarting") is True)
            time.sleep(4.5)
            ok = False
            for _ in range(120):
                try:
                    if httpx.get(BASE + "/api/health", timeout=1).status_code == 200:
                        ok = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            check("server back after restore", ok and srv.poll() is None)
            check("session survives restore", cl.req("GET", "/api/auth/status").json()["authenticated"])
            tree = cl.req("GET", f"/api/apps/{app_id}/files/tree").json()
            names = [f["name"] for f in tree]
            check("restore rolled files back", "main.py" in names and "after-backup.txt" not in names, str(names))
            apps = cl.req("GET", "/api/apps").json()
            check("env still decrypts after restore", "s3cr3t-value" in apps[0]["env_json"])
            check("safety backup was taken", len(cl.req("GET", "/api/system/backups").json()) == 2)

        # delete
        r = cl.req("DELETE", f"/api/apps/{app_id}")
        check("delete app", r.status_code == 200, r.text)
        check("app gone", cl.req("GET", "/api/apps").json() == [])
        print(f"\nALL {len(results)} CHECKS PASSED")
    finally:
        srv.send_signal(signal.SIGINT)
        try:
            srv.wait(15)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        if "--keep" in sys.argv or any(not ok for _, ok in results):
            print("data dir kept:", data)
            print((data / "server.log").read_text()[-3000:])
        else:
            shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    main()
