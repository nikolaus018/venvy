"""Deploying from git: settings, build step, restart, health-checked rollback,
manual rollback, push webhooks, snapshots of non-git apps, deploy keys."""
import hashlib, hmac, json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path
import httpx
from harness import Sink, admin, free_port, login_as, make_app, server
repo = sys.argv[1]; fails = []
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

work = Path(tempfile.mkdtemp(prefix="gitwork-"))
bare = work / "remote.git"; clone = work / "clone"
GENV = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}
def git(*a, cwd=clone):
    return subprocess.run(["git", *a], cwd=cwd, env=GENV, check=True, capture_output=True, text=True).stdout.strip()
def push(version, extra=None, message=None):
    (clone / "version.txt").write_text(version + "\n")
    for name, content in (extra or {}).items():
        (clone / name).write_text(content)
    git("add", "-A"); git("commit", "-q", "-m", message or f"version {version}"); git("push", "-q", "origin", "main")
    return git("rev-parse", "HEAD")

APP = '''\
import os, time
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        v = open("version.txt").read().strip()
        if self.path == "/health" and v == "bad":
            self.send_response(500); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(("V" + v).encode())
    def log_message(self, *a): pass
print("VERSION", open("version.txt").read().strip(), flush=True)
if open("version.txt").read().strip() == "crash":
    raise SystemExit(2)
HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
'''

subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, env=GENV)
subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True, env=GENV, capture_output=True)
git("checkout", "-q", "-B", "main")
(clone / "main.py").write_text(APP)
sha1 = push("1")

with server(repo, 55023, env={"VENVY_ALLOW_LOCAL_GIT": "1", "VENVY_HEALTH_WAIT_SEC": "20", "VENVY_METRICS_FLUSH_SEC": "5"}) as (base, d, srv):
    cl = admin(base)
    sink = Sink()
    cl.req("POST", "/api/system/notify", json={"name": "all", "kind": "webhook", "config": {"url": sink.url + "/n"}, "events": "*"})
    port = free_port()
    app = make_app(cl, name="Deployed", code="# placeholder\n", manual_ports=[port])
    put = lambda **b: cl.req("PUT", f"/api/apps/{app}/git", json=b)
    state = lambda: cl.req("GET", f"/api/apps/{app}/deploy").json()

    # ── settings ────────────────────────────────────────────────────────────
    for bad in ["ext::sh -c touch /tmp/pwned", "-uhttps://x", "ftp://x/y", "https://u:p@host/x.git", "https://host/a b", "git@host:path;rm -rf", "git://host/x"]:
        check(f"git url {bad!r} refused", put(git_url=bad).status_code == 400, "")
    sys.path.insert(0, repo)
    from venvy import deploy as dp
    def refused(u):
        try: dp.clean_git_url(u); return False
        except ValueError: return True
    check("local paths are refused unless the operator allows them", not dp.ALLOW_LOCAL_GIT and refused("/etc/passwd") and refused("file:///etc/passwd"))
    check("an over-long repository URL refused", put(git_url="https://github.com/" + "a" * 3000).status_code == 400)
    check("branch validated", put(git_branch="-x").status_code == 400 and put(git_branch="a..b").status_code == 400 and put(git_branch="x y").status_code == 400)
    check("token only for https", put(git_url=str(bare), token="abc").status_code == 400)
    check("ssh and https urls accepted", put(git_url="git@github.com:owner/repo.git").status_code == 200 and put(git_url="https://github.com/o/r.git", token="ghp_secrettoken").status_code == 200)
    check("token stored but never returned", "ghp_secrettoken" not in cl.req("GET", f"/api/apps/{app}/deploy").text and "ghp_secrettoken" not in json.dumps(cl.status(app)))
    check("app JSON hides git credentials", "git_auth" not in cl.status(app) and "webhook_secret" not in cl.status(app) and cl.status(app)["git_has_token"] is True)
    check("token removed", put(token="").json()["has_token"] is False)
    put(git_url="https://github.com/o/r.git", token="ghp_secrettoken")
    check("token kept when the host stays", put(git_url="https://github.com/o/other.git", git_branch="dev").json()["has_token"] is True)
    check("token dropped when the repository moves to another host",
          put(git_url="https://evil.example/o/r.git").json()["has_token"] is False)
    r = put(git_url=str(bare), git_branch="main", build_command="echo built-$(cat version.txt) > built.txt")
    check("local repo allowed here, build command saved", r.status_code == 200 and r.json()["build_command"].startswith("echo built-"), r.text)
    check("deploy refused with nothing configured", cl.req("POST", f"/api/apps/{make_app(cl, name='Bare')}/deploy").status_code == 400)

    def deploy(start=True, timeout=90):
        r = cl.req("POST", f"/api/apps/{app}/deploy", json={"start": start}); assert r.status_code == 200, r.text
        end = time.time() + timeout
        while time.time() < end:
            st = state()["state"]
            if not st["running"] and st["status"] not in (None, "running"):
                return st
            time.sleep(0.5)
        raise TimeoutError("deploy")
    ver = lambda: (httpx.get(f"http://127.0.0.1:{port}/", timeout=5).text if True else "")
    def read(path):
        r = cl.req("GET", f"/api/apps/{app}/files/content", params={"path": path})
        return r.text if r.status_code == 200 else None
    def file_text(path):
        r = cl.req("GET", f"/api/apps/{app}/files/download", params={"path": path})
        return r.text.strip() if r.status_code == 200 else None

    # ── first deploy ────────────────────────────────────────────────────────
    st = deploy()
    check("first deploy succeeds", st["status"] == "ok", "\n".join(st["log"][-15:]))
    check("... code and build output are there", file_text("version.txt") == "1" and file_text("built.txt") == "built-1")
    cl.wait_status(app, "running", 30); time.sleep(1.5)
    check("... and the app runs it", ver() == "V1")
    check("... log shows the steps", any("git fetch" in l for l in st["log"]) and any("build:" in l for l in st["log"]) and st["log"][-1] == "Finished: ok")
    s = state()
    check("last deploy recorded with its commit", s["last"]["status"] == "ok" and s["last"]["commit"] == sha1, str(s["last"]))
    check("a release was recorded", any(r["kind"] == "deploy" and r["commit"] == sha1 and r["message"] == "version 1" for r in s["releases"]), str(s["releases"]))
    check("deploy is audited and notified", any(e["action"] == "app_deploy" for e in cl.req("GET", "/api/audit?action=app_deploy").json()["entries"]) and bool(sink.wait(lambda g: b"deploy_ok" in g["body"], 10)))

    # ── a normal update ─────────────────────────────────────────────────────
    sha2 = push("2")
    st = deploy()
    check("update deploys and restarts", st["status"] == "ok" and file_text("version.txt") == "2" and file_text("built.txt") == "built-2")
    time.sleep(1.5); check("... new version is what runs", ver() == "V2")

    # ── a version that does not start is rolled back ────────────────────────
    sink.got.clear()
    sha3 = push("crash")
    st = deploy(timeout=120)
    check("crashing deploy is rolled back", st["status"] == "rolled-back", "\n".join(st["log"][-12:]))
    check("... previous code is back", file_text("version.txt") == "2")
    cl.wait_status(app, "running", 30); time.sleep(1.5)
    check("... and the previous version runs again", ver() == "V2")
    check("... with a notification", bool(sink.wait(lambda g: b"deploy_rolled_back" in g["body"], 15)))
    check("... and the failed commit is not marked as deployed", state()["last"]["status"] == "rolled-back" and state()["last"]["commit"] is None)

    # ── a failing health check rolls back too ───────────────────────────────
    cl.req("PUT", f"/api/apps/{app}", json={"healthcheck_path": "/health", "health_interval": 5})
    sha4 = push("bad")
    st = deploy(timeout=120)
    check("a version failing its health check is rolled back", st["status"] == "rolled-back" and file_text("version.txt") == "2", "\n".join(st["log"][-10:]))
    cl.req("PUT", f"/api/apps/{app}", json={"healthcheck_path": ""})

    # ── a failing build leaves the running version alone ────────────────────
    cl.req("PUT", f"/api/apps/{app}/git", json={"build_command": "echo compiling; exit 1"})
    push("5")
    st = deploy()
    check("failed build: deploy fails, nothing restarted", st["status"] == "failed" and any("exit code 1" in l for l in st["log"]) and file_text("version.txt") == "2", "\n".join(st["log"][-8:]))
    check("... the app still serves the old version", ver() == "V2")
    cl.req("PUT", f"/api/apps/{app}/git", json={"build_command": "echo built-$(cat version.txt) > built.txt"})

    # ── manual rollback ─────────────────────────────────────────────────────
    rels = state()["releases"]
    target = next(r for r in rels if r["kind"] == "deploy" and r["commit"] == sha1)
    r = cl.req("POST", f"/api/apps/{app}/releases/{target['id']}/rollback"); check("rollback accepted", r.status_code == 200, r.text)
    end = time.time() + 90
    while time.time() < end and (state()["state"]["running"] or state()["state"]["trigger"] != "rollback"): time.sleep(0.5)
    time.sleep(1)
    check("rollback restores the old commit and rebuilds", file_text("version.txt") == "1" and file_text("built.txt") == "built-1", str(state()["state"]["log"][-6:]))
    time.sleep(1.5); check("... and the app runs it", ver() == "V1")
    check("rollback needs a restorable release of this app", cl.req("POST", f"/api/apps/{app}/releases/99999/rollback").status_code == 404)
    check("redeploying after a rollback goes forward again", deploy()["status"] == "ok" and file_text("version.txt") == "5")

    # ── webhooks ────────────────────────────────────────────────────────────
    g = put(auto_deploy=True).json()
    secret = g["webhook_secret"]; hook = base + g["webhook_path"]
    check("auto-deploy creates a webhook secret", bool(secret) and g["auto_deploy"] is True)
    sha6 = push("6")
    body = json.dumps({"ref": "refs/heads/main", "after": sha6}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    check("no signature: 401", httpx.post(hook, content=body).status_code == 401)
    check("wrong signature: 401", httpx.post(hook, content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401)
    check("an unknown app answers the same", httpx.post(base + "/api/hooks/deploy/9999", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 401)
    check("ping answers pong", httpx.post(hook, content=b"{}", headers={"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), b"{}", hashlib.sha256).hexdigest(), "X-GitHub-Event": "ping"}).json().get("pong") is True)
    other = json.dumps({"ref": "refs/heads/feature"}).encode()
    r = httpx.post(hook, content=other, headers={"X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), other, hashlib.sha256).hexdigest()})
    check("a push to another branch is ignored", r.status_code == 200 and r.json()["deployed"] is False and "feature" in r.json()["reason"], r.text)
    def signed(payload):
        raw = json.dumps(payload).encode()
        return httpx.post(hook, content=raw, headers={"X-GitHub-Event": "push",
                          "X-Hub-Signature-256": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()})
    r = signed({"ref": "refs/tags/v1.0", "after": sha6})
    check("a tag push is not a deploy", r.status_code == 200 and r.json()["deployed"] is False and "refs/tags/v1.0" in r.json()["reason"], r.text)
    r = signed({"ref": "refs/heads/main", "after": "0" * 40, "deleted": True})
    check("a push that deletes the branch is not a deploy", r.status_code == 200 and r.json()["deployed"] is False and "deleted" in r.json()["reason"], r.text)
    r = httpx.post(hook, content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"})
    check("an event other than a push is ignored", r.status_code == 200 and r.json()["deployed"] is False and "issues" in r.json()["reason"], r.text)
    r = httpx.post(hook, content=body, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "push"})
    check("a signed push deploys", r.status_code == 200 and r.json()["deployed"] is True, r.text)
    end = time.time() + 90
    while time.time() < end and (state()["state"]["trigger"] != "webhook" or state()["state"]["running"]): time.sleep(0.5)
    check("... and the new code is live", file_text("version.txt") == "6" and state()["state"]["status"] == "ok")
    sha7 = push("7")
    r = httpx.post(hook, content=b'{"ref": "refs/heads/main"}', headers={"X-Gitlab-Token": secret})
    check("GitLab-style token works", r.status_code == 200 and r.json()["deployed"] is True, r.text)
    end = time.time() + 90
    while time.time() < end and (state()["state"]["running"] or file_text("version.txt") != "7"): time.sleep(0.5)
    sha8 = push("8")
    r = httpx.post(hook, content=b"{}", headers={"Authorization": f"Bearer {secret}"})
    check("Bearer secret works (own CI)", r.status_code == 200 and r.json()["deployed"] is True, r.text)
    end = time.time() + 90
    while time.time() < end and (state()["state"]["running"] or file_text("version.txt") != "8"): time.sleep(0.5)
    r = httpx.post(hook, content=b'["not", "an", "object"]', headers={"Authorization": f"Bearer {secret}"})
    check("a JSON body that is not an object names no branch (no 500)", r.status_code == 200 and r.json()["deployed"] is True, r.text)
    end = time.time() + 90
    while time.time() < end and state()["state"]["running"]: time.sleep(0.5)
    check("webhook deploys are audited", any(e["user"] == "webhook" for e in cl.req("GET", "/api/audit?action=app_webhook").json()["entries"]))
    put(auto_deploy=False)
    r = httpx.post(hook, content=b"{}", headers={"Authorization": f"Bearer {secret}"})
    check("auto-deploy off: authenticated but not deployed", r.status_code == 200 and r.json()["deployed"] is False)
    new_secret = cl.req("POST", f"/api/apps/{app}/git/webhook-secret").json()["webhook_secret"]
    check("regenerated secret replaces the old", new_secret != secret and httpx.post(hook, content=b"{}", headers={"Authorization": f"Bearer {secret}"}).status_code == 401)

    # ── deploy key ──────────────────────────────────────────────────────────
    if shutil.which("ssh-keygen"):
        k = cl.req("POST", f"/api/apps/{app}/git/key")
        check("deploy key generated", k.status_code == 200 and k.json()["public_key"].startswith("ssh-ed25519 "), k.text)
        put(git_url="git@example.invalid:o/r.git")
        check("... and shown with the git settings", state()["git"]["public_key"] == k.json()["public_key"] and state()["git"]["ssh"] is True)
        check("the private half is not served anywhere", "PRIVATE" not in cl.req("GET", f"/api/apps/{app}/deploy").text)
        put(git_url=str(bare))

    # ── permissions ─────────────────────────────────────────────────────────
    cl.req("POST", "/api/users", json={"username": "viewer1", "password": "hunter2hunter2", "role": "viewer"})
    vw = login_as(base, "viewer1", "hunter2hunter2")
    vg = vw.req("GET", f"/api/apps/{app}/deploy").json()
    check("a viewer sees deploy state but not the webhook secret", vg["git"]["webhook_secret"] is None and vg["state"] is not None)
    check("... nor the build command (it may carry a secret, like a job's)", vg["git"]["build_command"] == "" and state()["git"]["build_command"] != "")
    rtok = cl.req("POST", "/api/tokens", json={"name": "ro", "scope": "read"}).json()["token"]
    tg = httpx.get(f"{base}/api/apps/{app}/deploy", headers={"Authorization": f"Bearer {rtok}"}).json()
    check("an API token does not get the webhook secret", tg["git"]["webhook_secret"] is None and tg["git"]["has_webhook_secret"] is True, str(tg["git"]))
    check("a viewer cannot deploy or change git settings", vw.req("POST", f"/api/apps/{app}/deploy").status_code == 403 and vw.req("PUT", f"/api/apps/{app}/git", json={"git_branch": "x"}).status_code == 403)

    # ── an app that is not a git checkout: snapshots ────────────────────────
    snap = make_app(cl, name="Snap", code='print("ORIGINAL")\nimport time; time.sleep(120)\n')
    cl.req("PUT", f"/api/apps/{snap}", json={"env_vars": {"API_KEY": "sk-live-abcdef123", "DEBUG": "1"}})
    cl.req("PUT", f"/api/apps/{snap}/git", json={"build_command": "echo rebuilt > built.txt; echo key=$API_KEY"})
    r = cl.req("POST", f"/api/apps/{snap}/deploy", json={"start": False}); check("build-only deploy accepted", r.status_code == 200)
    end = time.time() + 60
    while time.time() < end and cl.req("GET", f"/api/apps/{snap}/deploy").json()["state"]["status"] in (None, "running"): time.sleep(0.5)
    sd = cl.req("GET", f"/api/apps/{snap}/deploy").json()
    check("build-only deploy runs the build", sd["state"]["status"] == "ok" and any("build:" in l for l in sd["state"]["log"]))
    before = [r for r in sd["releases"] if r["kind"] == "before-deploy"]
    check("a snapshot is taken first", len(before) == 1 and before[0]["size"] > 0 and before[0]["commit"] is None, str(sd["releases"]))
    check("only restorable releases are recorded", sd["releases"] and all(r["restorable"] for r in sd["releases"]), str(sd["releases"]))
    log_text = "\n".join(sd["state"]["log"])
    check("variable values printed by the build are masked in the log", "key=***" in log_text and "sk-live-abcdef123" not in log_text, log_text[-400:])
    cl.req("POST", f"/api/apps/{snap}/files/op", json={"operation": "save", "path": "main.py", "content": 'print("CHANGED")\n'})
    cl.req("POST", f"/api/apps/{snap}/files/op", json={"operation": "save", "path": "keep.txt", "content": "stays"})
    r = cl.req("POST", f"/api/apps/{snap}/releases/{before[0]['id']}/rollback"); check("snapshot rollback accepted", r.status_code == 200)
    end = time.time() + 60
    while time.time() < end and (cl.req("GET", f"/api/apps/{snap}/deploy").json()["state"]["running"] or cl.req("GET", f"/api/apps/{snap}/deploy").json()["state"]["trigger"] != "rollback"): time.sleep(0.5)
    time.sleep(1)
    back = cl.req("GET", f"/api/apps/{snap}/files/download", params={"path": "main.py"}).text
    check("snapshot restores the files as they were", "ORIGINAL" in back and "CHANGED" not in back, back)
    check("... and does not delete what came later", cl.req("GET", f"/api/apps/{snap}/files/download", params={"path": "keep.txt"}).status_code == 200)
    m = cl.req("POST", f"/api/apps/{snap}/releases"); cl.job(m)
    check("a manual snapshot can be taken", any(r["kind"] == "manual" for r in cl.req("GET", f"/api/apps/{snap}/releases").json()))
    check("... without wiping the last deploy's log file", "Finished:" in (d / "deploy-logs" / f"{snap}.log").read_text())

    # ── releases can be deleted, and old ones go away by themselves ─────────
    rl = cl.req("GET", f"/api/apps/{snap}/releases").json()
    snap_rel = next(r for r in rl if r["kind"] == "manual")
    arch_dir = d / "releases" / str(snap)
    n_files = len(list(arch_dir.glob("*")))
    check("deleting a release needs it to exist", cl.req("DELETE", f"/api/apps/{snap}/releases/99999").status_code == 404)
    check("a release of another app is not deletable", cl.req("DELETE", f"/api/apps/{app}/releases/{snap_rel['id']}").status_code == 404)
    check("a release can be deleted", cl.req("DELETE", f"/api/apps/{snap}/releases/{snap_rel['id']}").status_code == 200)
    check("... it is gone from the list and its snapshot file is removed",
          all(r["id"] != snap_rel["id"] for r in cl.req("GET", f"/api/apps/{snap}/releases").json()) and len(list(arch_dir.glob("*"))) == n_files - 1)
    check("keep_releases is shown", cl.req("GET", f"/api/apps/{snap}/deploy").json()["git"]["keep_releases"] == 10)
    check("keep_releases must be 1-50", cl.req("PUT", f"/api/apps/{snap}/git", json={"keep_releases": 0}).status_code == 400
          and cl.req("PUT", f"/api/apps/{snap}/git", json={"keep_releases": 51}).status_code == 400)
    for _ in range(3):
        cl.job(cl.req("POST", f"/api/apps/{snap}/releases"))
    check("several snapshots are kept", len(cl.req("GET", f"/api/apps/{snap}/releases").json()) >= 3)
    check("keep_releases is saved", cl.req("PUT", f"/api/apps/{snap}/git", json={"keep_releases": 2}).json()["keep_releases"] == 2)
    left = cl.req("GET", f"/api/apps/{snap}/releases").json()
    check("lowering it deletes the older releases at once, files included", len(left) == 2 and len(list(arch_dir.glob("*"))) == 2, str(left))
    cl.job(cl.req("POST", f"/api/apps/{snap}/releases"))
    check("a new release pushes the oldest out", len(cl.req("GET", f"/api/apps/{snap}/releases").json()) == 2 and len(list(arch_dir.glob("*"))) == 2)
    # The oldest kept release is still there when a rollback to it restores it,
    # although the rollback's own before-deploy snapshot pushes it past the limit.
    oldest = cl.req("GET", f"/api/apps/{snap}/releases").json()[-1]
    prev_fin = cl.req("GET", f"/api/apps/{snap}/deploy").json()["state"]["finished"]
    check("rollback to the oldest kept release accepted", cl.req("POST", f"/api/apps/{snap}/releases/{oldest['id']}/rollback").status_code == 200)
    end = time.time() + 60
    while time.time() < end:
        st = cl.req("GET", f"/api/apps/{snap}/deploy").json()["state"]
        if not st["running"] and st["finished"] != prev_fin: break
        time.sleep(0.5)
    check("... restores it instead of pruning it first", st["status"] == "ok", str(st["log"][-6:]))
    check("... and the limit holds afterwards", len(cl.req("GET", f"/api/apps/{snap}/releases").json()) == 2 and len(list(arch_dir.glob("*"))) == 2)

    # ── the first deploy from git over uploaded files can be undone ─────────
    fresh = make_app(cl, name="Fresh", code='print("UPLOADED")\n')
    cl.req("PUT", f"/api/apps/{fresh}/git", json={"git_url": str(bare), "git_branch": "main", "build_command": "exit 1"})
    r = cl.req("POST", f"/api/apps/{fresh}/deploy", json={"start": False}); check("first git deploy accepted", r.status_code == 200, r.text)
    end = time.time() + 60
    while time.time() < end and cl.req("GET", f"/api/apps/{fresh}/deploy").json()["state"]["status"] in (None, "running"): time.sleep(0.5)
    fd = cl.req("GET", f"/api/apps/{fresh}/deploy").json()
    back = cl.req("GET", f"/api/apps/{fresh}/files/download", params={"path": "main.py"}).text
    fresh_dirs = list(d.glob("apps/*/fresh*"))
    check("a failed first git deploy puts the uploaded files back", fd["state"]["status"] == "failed" and "UPLOADED" in back, back[:200] + str(fd["state"]["log"][-6:]))
    check("... and removes the checkout it made", fresh_dirs and not (fresh_dirs[0] / ".git").exists(), str(fresh_dirs))

    # ── delete cleans up ────────────────────────────────────────────────────
    check("deleting an app removes its releases and keys", cl.req("DELETE", f"/api/apps/{snap}").status_code == 200 and not (d / "releases" / str(snap)).exists())
    cl.req("DELETE", f"/api/apps/{fresh}")
    reused = make_app(cl, name="Reused")
    check("an app that gets a deleted app's id starts with no deploy log", reused not in (fresh, snap)
          or cl.req("GET", f"/api/apps/{reused}/deploy").json()["state"]["log"] == [], f"id {reused}")
    sink.close()

shutil.rmtree(work, ignore_errors=True)
print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
