"""Deploys beyond a git pull: venvy.toml / Procfile from the repository,
release commands, atomic deploys that switch back, artifact deploys (and
ship.py deploy), the deploy queue, preview apps per branch, app backups with
consistent SQLite copies, and managing a second Venvy server."""
import hashlib, hmac, io, json, os, subprocess, sys, tarfile, tempfile, time
from pathlib import Path
import httpx
from harness import admin, free_port, server

repo = sys.argv[1]
fails = []


def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c:
        fails.append(n)


work = Path(tempfile.mkdtemp(prefix="plat2-"))
bare, clone = work / "remote.git", work / "clone"
GENV = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}


def git(*a, cwd=clone):
    return subprocess.run(["git", *a], cwd=cwd, env=GENV, check=True, capture_output=True, text=True).stdout.strip()


APP = '''\
import os, sqlite3, time
from http.server import BaseHTTPRequestHandler, HTTPServer
db = sqlite3.connect(os.path.join(os.environ["APP_DATA_DIR"], "app.db"), check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL"); db.execute("create table if not exists hits(x)"); db.commit()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/hit":
            db.execute("insert into hits values (1)"); db.commit()
        n = db.execute("select count(*) from hits").fetchone()[0]
        v = open("version.txt").read().strip()
        if self.path == "/health" and v == "bad":
            self.send_response(500); self.end_headers(); return
        body = f"V{v} HITS={n} MIG={os.path.exists('migrated.txt')} P={os.environ.get('VENVY_PROCESS', 'web')} E={os.environ.get('FROM_TOML', '')}"
        self.send_response(200); self.end_headers(); self.wfile.write(body.encode())
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''

subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, env=GENV)
subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, env=GENV, capture_output=True)
git("checkout", "-q", "-B", "main")
(clone / "main.py").write_text(APP)
(clone / "version.txt").write_text("1\n")
(clone / "venvy.toml").write_text(f'''runtime = "command"
start = "{sys.executable} main.py"
release = "echo ran > migrated.txt"
[health]
path = "/health"
[processes]
worker = "{sys.executable} -c 'import time; print(\\"WORKER-RUNS\\", flush=True); time.sleep(600)'"
[env]
FROM_TOML = "toml-value"
''')
git("add", "-A")
git("commit", "-q", "-m", "v1")
git("push", "-q", "origin", "main")

with server(repo, 55024, env={"VENVY_ALLOW_LOCAL_GIT": "1", "VENVY_HEALTH_WAIT_SEC": "20"}) as (base, data, srv):
    cl = admin(base)

    def new_app(name, **settings):
        r = cl.req("POST", "/api/apps/upload", data={"name": name, "runtime": "command", "start_command": "true"})
        aid = r.json()["app_id"]
        cl.job(r)
        if settings:
            rr = cl.req("PUT", f"/api/apps/{aid}", json=settings)
            assert rr.status_code == 200, rr.text
        return aid

    def deploy_and_wait(aid, timeout=120, **body):
        r = cl.req("POST", f"/api/apps/{aid}/deploy", json={"start": True, **body})
        assert r.status_code == 200, r.text
        return wait_deploy(aid, timeout)

    def wait_deploy(aid, timeout=120):
        time.sleep(0.5)
        end = time.time() + timeout
        while time.time() < end:
            st = cl.req("GET", f"/api/apps/{aid}/deploy").json()["state"]
            if not st["running"] and st["status"] not in (None, "running"):
                return st
            time.sleep(0.5)
        return cl.req("GET", f"/api/apps/{aid}/deploy").json()["state"]

    def get(port, path="/"):
        for _ in range(40):
            try:
                return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=3)
            except Exception:
                time.sleep(0.3)

    port = free_port()
    aid = new_app("Spec", manual_ports=[port])
    cl.req("PUT", f"/api/apps/{aid}/git", json={"git_url": str(bare), "git_branch": "main"})

    # ── venvy.toml, Procfile-style workers, release command ──────────────
    st = deploy_and_wait(aid)
    check("deploy from git with venvy.toml succeeds", st["status"] == "ok", "\n".join(st["log"][-20:]))
    a = cl.status(aid)
    check("venvy.toml set the start command and health check", a["start_command"].endswith("main.py") and a["healthcheck_path"] == "/health")
    r = get(port)
    check("release command ran before the start", r is not None and "MIG=True" in r.text, getattr(r, "text", ""))
    check("[env] from venvy.toml fills in variables", r is not None and "E=toml-value" in r.text)
    procs = cl.req("GET", f"/api/apps/{aid}/processes").json()
    check("[processes] made a worker", any(p["process_name"] == "worker" for p in procs), str(procs))

    # ── a failing release command stops the deploy before the restart ────
    (clone / "venvy.toml").write_text((clone / "venvy.toml").read_text().replace('release = "echo ran > migrated.txt"', 'release = "exit 3"'))
    (clone / "version.txt").write_text("2\n")
    git("commit", "-qam", "v2 broken release")
    git("push", "-q", "origin", "main")
    st = deploy_and_wait(aid)
    check("failing release command fails the deploy", st["status"] == "failed", st["status"])
    check("... and the running app is still the old version", "V1" in get(port).text, get(port).text)

    # ── atomic deploy, switching back when the app is not healthy ────────
    (clone / "venvy.toml").write_text((clone / "venvy.toml").read_text().replace('release = "exit 3"', 'release = "true"'))
    (clone / "version.txt").write_text("bad\n")
    git("commit", "-qam", "bad health")
    git("push", "-q", "origin", "main")
    cl.req("PUT", f"/api/apps/{aid}", json={"deploy_strategy": "atomic"})
    for _ in range(3):
        get(port, "/hit")
    st = deploy_and_wait(aid)
    check("atomic deploy of an unhealthy version is switched back", st["status"] == "rolled-back", "\n".join(st["log"][-15:]))
    r = get(port)
    check("... the old build is running again, data intact", r is not None and "V1" in r.text and "HITS=3" in r.text, getattr(r, "text", ""))
    check("... the build was made next to the app, then switched", any("Switching to the new build" in ln for ln in st["log"]))
    (clone / "version.txt").write_text("3\n")
    git("commit", "-qam", "v3")
    git("push", "-q", "origin", "main")
    st = deploy_and_wait(aid)
    check("atomic deploy of a good version", st["status"] == "ok" and "V3" in get(port).text, "\n".join(st["log"][-10:]))

    # ── the deploy queue ─────────────────────────────────────────────────
    r1 = cl.req("POST", f"/api/apps/{aid}/deploy", json={})
    r2 = cl.req("POST", f"/api/apps/{aid}/deploy", json={})
    check("a second deploy while one runs is queued", r2.status_code == 200 and r2.json().get("queued") is True, r2.text)
    st = wait_deploy(aid)
    time.sleep(2)
    st = wait_deploy(aid)
    check("... and runs afterwards", "(queued)" in (st.get("trigger") or ""), str(st.get("trigger")))

    # ── app backup with a consistent SQLite copy, and restore ────────────
    for _ in range(5):
        get(port, "/hit")
    before = get(port).text
    r = cl.req("POST", f"/api/apps/{aid}/backups")
    res = cl.job(r, timeout=120)
    check("app backup made", res.get("name", "").startswith(f"app-{aid}-"), str(res))
    for _ in range(4):
        get(port, "/hit")
    rr = cl.req("POST", f"/api/apps/{aid}/backups/{res['name']}/restore", json={})
    cl.job(rr, timeout=120)
    time.sleep(1)
    after = get(port)
    hits_before = before.split("HITS=")[1].split()[0]
    check("restore puts the database back as it was (WAL included)", after is not None and f"HITS={hits_before}" in after.text,
          f"{before} -> {getattr(after, 'text', None)}")
    check("bad backup name refused", cl.req("POST", f"/api/apps/{aid}/backups/../../x/restore", json={}).status_code in (404, 405))

    # ── artifact deploy (and ship.py deploy) ─────────────────────────────
    port2 = free_port()
    art = new_app("Artifact", manual_ports=[port2], start_command=f"{sys.executable} main.py")
    src = work / "art"
    src.mkdir()
    (src / "main.py").write_text(APP)
    (src / "version.txt").write_text("A1\n")
    (src / "old.txt").write_text("gone soon")

    def upload(d, start=True):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for f in d.iterdir():
                tf.add(f, arcname=f.name)
        return cl.req("POST", f"/api/apps/{art}/deploy/artifact?start={'true' if start else 'false'}",
                      files={"file": ("a.tar.gz", buf.getvalue(), "application/gzip")})
    r = upload(src)
    check("artifact accepted", r.status_code == 200, r.text)
    st = wait_deploy(art)
    check("artifact deployed and running", st["status"] == "ok" and "VA1" in get(port2).text, "\n".join(st["log"][-10:]))
    cl.req("POST", f"/api/apps/{art}/files/op", json={"operation": "save", "path": ".env", "content": "KEEP=1"})
    (src / "old.txt").unlink()
    (src / "version.txt").write_text("A2\n")
    upload(src)
    st = wait_deploy(art)
    tree = json.dumps(cl.req("GET", f"/api/apps/{art}/files/tree").json())
    check("artifact replaces the tree: removed files are gone", "old.txt" not in tree and "VA2" in get(port2).text, tree[:300])
    check("... data files carry over", ".env" in tree)
    rels = cl.req("GET", f"/api/apps/{art}/releases").json()
    check("artifact deploys are releases to roll back to", sum(1 for x in rels if x["restorable"]) >= 2, str(rels))
    target = [x for x in rels if x["kind"] == "deploy"][-1]
    rb = cl.req("POST", f"/api/apps/{art}/releases/{target['id']}/rollback")
    st = wait_deploy(art)
    check("roll back to the first artifact", rb.status_code == 200 and "VA1" in get(port2).text, getattr(get(port2), "text", ""))
    bad = io.BytesIO()
    with tarfile.open(fileobj=bad, mode="w:gz") as tf:
        ti = tarfile.TarInfo("../evil")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    r = cl.req("POST", f"/api/apps/{art}/deploy/artifact", files={"file": ("a.tar.gz", bad.getvalue(), "application/gzip")})
    check("artifact with ../ refused", r.status_code == 400, r.text)
    check("artifact that is not a tarball refused",
          cl.req("POST", f"/api/apps/{art}/deploy/artifact", files={"file": ("a.tar.gz", b"nope", "application/gzip")}).status_code == 400)
    # ship.py deploy from a git checkout
    cfg = Path(tempfile.mkdtemp())
    env = {**os.environ, "HOME": str(cfg), "VENVY_URL": base}
    (cfg / ".config" / "venvy").mkdir(parents=True)
    host = base.split("://")[1].replace(":", "_")
    (cfg / ".config" / "venvy" / f"{host}.json").write_text(json.dumps({"auth_key": cl.key, "session": cl.c.cookies.get("session")}))
    (clone / "version.txt").write_text("SHIP\n")
    git("commit", "-qam", "ship")
    sp = subprocess.run([sys.executable, str(Path(repo, "scripts/ship.py")), "deploy", str(art), "--dir", str(clone), "--wait"],
                        env=env, capture_output=True, text=True, timeout=180)
    check("ship.py deploy --wait", sp.returncode == 0 and "Result: ok" in sp.stdout and "VSHIP" in get(port2).text,
          sp.stdout[-500:] + sp.stderr[-500:])

    # ── previews per branch ──────────────────────────────────────────────
    secret = cl.req("POST", f"/api/apps/{aid}/git/webhook-secret").json()["webhook_secret"]
    cl.req("PUT", f"/api/apps/{aid}/git", json={"auto_deploy": True})
    cl.req("PUT", f"/api/apps/{aid}", json={"previews_enabled": True, "preview_domain": "pr.test", "deploy_strategy": "in_place",
                                           "preview_env": {"FROM_TOML": "preview-value"}})
    git("checkout", "-q", "-b", "feature/x")
    (clone / "version.txt").write_text("F\n")
    git("commit", "-qam", "feature")
    git("push", "-q", "origin", "feature/x")

    def hook(payload):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return httpx.post(f"{base}/api/hooks/deploy/{aid}", content=body,
                          headers={"x-github-event": "push", "x-hub-signature-256": sig, "content-type": "application/json"})
    hr = hook({"ref": "refs/heads/feature/x", "after": "1" * 40})
    check("push of another branch makes a preview", hr.status_code == 200 and hr.json().get("preview"), hr.text)
    pid = hr.json().get("preview")
    end = time.time() + 120
    pv = None
    while time.time() < end:
        pv = next((x for x in cl.req("GET", f"/api/apps/{aid}/previews").json() if x["id"] == pid), None)
        if pv and pv["last_deploy_status"] in ("ok", "failed", "rolled-back") and pv["status"] == "running":
            break
        time.sleep(1)
    check("preview deployed, on its own domain and branch", pv and pv["last_deploy_status"] == "ok" and pv["domain"] == "feature-x.pr.test"
          and pv["git_branch"] == "feature/x" and pv["environment"] == "preview", str(pv and {k: pv[k] for k in ("last_deploy_status", "domain", "status")}))
    rr = httpx.get(base + "/", headers={"host": "feature-x.pr.test"}, timeout=10)
    check("preview served on <branch>.<preview domain> with its overrides", "VF" in rr.text and "E=preview-value" in rr.text, rr.text[:200])
    hr = hook({"ref": "refs/heads/feature/x", "after": "0" * 40, "deleted": True})
    gone = False
    for _ in range(90):
        if not any(x["id"] == pid for x in cl.req("GET", "/api/apps").json()):
            gone = True
            break
        time.sleep(1)
    check("deleting the branch deletes the preview", hr.json().get("deleted") is True and gone, hr.text)

    # ── a second server, managed from this one ───────────────────────────
    with server(repo, 55025) as (base2, data2, srv2):
        cl2 = admin(base2)
        r = cl2.req("POST", "/api/apps/upload", data={"name": "Remote one", "runtime": "command",
                                                      "start_command": f"{sys.executable} -c \"import time; time.sleep(600)\""})
        rid = r.json()["app_id"]
        cl2.job(r)
        token = cl2.req("POST", "/api/tokens", json={"name": "fed", "scope": "full"}).json()["token"]
        r = cl.req("POST", "/api/servers", json={"name": "second", "url": base2, "token": token})
        check("remote server added", r.status_code == 200 and r.json()["status"] == "ok", r.text)
        sid = r.json().get("id")
        apps = cl.req("GET", "/api/servers/apps").json()
        check("remote apps listed", any(x.get("name") == "Remote one" and x["server"] == "second" for x in apps), str(apps))
        r = cl.req("POST", f"/api/servers/{sid}/apps/{rid}/start")
        check("remote app started from here", r.status_code == 200 and cl2.wait_status(rid, "running"), r.text)
        check("token kept encrypted, never returned", token not in json.dumps(cl.req("GET", "/api/servers").json()))
        bad = cl.req("POST", "/api/servers", json={"name": "bad", "url": base2, "token": "wrong"}).json()
        check("a wrong token shows as an error", bad.get("status") != "ok", str(bad))
        check("bad action refused", cl.req("POST", f"/api/servers/{sid}/apps/{rid}/explode").status_code == 400)

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
