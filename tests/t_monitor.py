"""Health checks, resource history, alerts, disk usage, scheduled jobs, scheduled
backups and the off-site copy. Several of these take a minute of real time (a
schedule fires on a minute boundary), so everything is set up first and checked
after one wait."""
import base64, hashlib, json, sys, time
import httpx
from harness import Sink, SERVER_APP, admin, free_port, login_as, make_app, server
repo = sys.argv[1]; fails = []
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

with server(repo, 55022, env={"VENVY_METRICS_FLUSH_SEC": "5"}) as (base, d, srv):
    cl = admin(base)
    sink = Sink()
    r = cl.req("POST", "/api/system/notify", json={"name": "all", "kind": "webhook", "config": {"url": sink.url + "/n"}, "events": "*"})
    def events(name, timeout=1, has=None):
        """Events of this kind (whose title/body mention `has`), waiting up to `timeout` for one."""
        pred = lambda g: g["path"] == "/n" and name.encode() in g["body"] and (has is None or has.encode() in g["body"])
        return [json.loads(g["body"]) for g in sink.wait(pred, timeout)]

    # ── set up: a health-checked app ────────────────────────────────────────
    port = free_port()
    web = make_app(cl, name="Web", code=SERVER_APP, manual_ports=[port], env_vars={"MARK": "hello"})
    chk = lambda **b: cl.req("PUT", f"/api/apps/{web}", json=b)
    check("health path must be a path", chk(healthcheck_path="http://evil/x").status_code == 400 and chk(healthcheck_path="/a b").status_code == 400)
    check("health interval range", chk(health_interval=1).status_code == 400 and chk(health_interval=99999).status_code == 400)
    check("health failures range", chk(health_failures=0).status_code == 400 and chk(health_failures=99).status_code == 400)
    r = chk(healthcheck_path="health", health_interval=5, health_failures=2)
    check("health settings saved (path normalised)", r.status_code == 200 and r.json()["healthcheck_path"] == "/health" and r.json()["health_interval"] == 5)
    cl.req("POST", f"/api/apps/{web}/control?action=start"); cl.wait_status(web, "running")
    t_start = time.time()

    # ── set up: alerts ──────────────────────────────────────────────────────
    ar = lambda **b: cl.req("POST", "/api/system/alerts", json=b)
    check("alert metric validated", ar(app_id=0, metric="pigeons", threshold=5, minutes=1).status_code == 400)
    check("host threshold is a percentage", ar(app_id=0, metric="cpu", threshold=250, minutes=1).status_code == 400)
    check("minutes validated", ar(app_id=0, metric="cpu", threshold=50, minutes=0).status_code == 400)
    check("unknown app refused", ar(app_id=999, metric="cpu", threshold=50, minutes=1).status_code == 404)
    rule = ar(app_id=web, metric="memory", threshold=1, minutes=1).json()   # 1 MB: any Python process is over it
    check("alert created and described", "above 1 MB" in rule["description"] and rule["firing"] is False, str(rule))
    quiet = ar(app_id=0, metric="disk", threshold=100, minutes=1).json()
    check("alerts listed", len(cl.req("GET", "/api/system/alerts").json()) == 2)

    # ── set up: jobs ────────────────────────────────────────────────────────
    jb = lambda **b: cl.req("POST", f"/api/apps/{web}/jobs", json={"name": "j", "schedule": "* * * * *", "command": "echo hi", **b})
    check("bad cron refused", jb(schedule="* * *").status_code == 400 and jb(schedule="61 * * * *").status_code == 400 and jb(schedule="0 0 31 2 *").status_code == 400)
    check("timeout range", jb(timeout=1).status_code == 400 and jb(timeout=999999).status_code == 400)
    check("name and command required", jb(name=" ").status_code == 400 and jb(command=" ").status_code == 400)
    cron_job = jb(name="every minute", command='echo "JOB-RAN $MARK $SHARED_X"; pwd').json()
    check("job created with next run", cron_job["next_run"] and cron_job["schedule"] == "* * * * *", str(cron_job))
    cl.req("PUT", "/api/system/shared-env", json={"vars": {"SHARED_X": "shared-ok"}})
    now_job = jb(name="now", schedule="0 3 * * *", command='echo "OUT"; echo "ERR" >&2').json()
    r = cl.req("POST", f"/api/apps/{web}/jobs/{now_job['id']}/run"); check("run now accepted", r.status_code == 200)
    time.sleep(2)
    j = next(x for x in cl.req("GET", f"/api/apps/{web}/jobs").json() if x["id"] == now_job["id"])
    check("run now: output (both streams) and status", j["last_status"] == "ok" and "OUT" in j["last_output"] and "ERR" in j["last_output"] and j["last_exit_code"] == 0, str(j))
    bad = jb(name="fails", schedule="0 3 * * *", command="echo before-fail; exit 3").json()
    cl.req("POST", f"/api/apps/{web}/jobs/{bad['id']}/run"); time.sleep(2)
    j = next(x for x in cl.req("GET", f"/api/apps/{web}/jobs").json() if x["id"] == bad["id"])
    check("failing job is recorded", j["last_status"] == "failed" and j["last_exit_code"] == 3 and "before-fail" in j["last_output"], str(j))
    ev = events("job_failed", 15)
    check("failing job notifies", bool(ev) and "fails" in ev[0]["title"] and "before-fail" in ev[0]["body"], str(ev))
    slow = jb(name="slow", schedule="0 3 * * *", command="sleep 40", timeout=5).json()
    cl.req("POST", f"/api/apps/{web}/jobs/{slow['id']}/run")
    check("a running job is not started twice", cl.req("POST", f"/api/apps/{web}/jobs/{slow['id']}/run").status_code == 409)
    time.sleep(8)
    j = next(x for x in cl.req("GET", f"/api/apps/{web}/jobs").json() if x["id"] == slow["id"])
    check("job over its timeout is killed", j["last_status"] == "timeout" and "killed" in j["last_output"] and (j["last_duration"] or 0) < 15, str(j))
    cl.req("PUT", f"/api/apps/{web}/jobs/{slow['id']}", json={"name": "slow", "schedule": "0 4 * * *", "command": "true", "timeout": 30, "enabled": False})
    check("job updated (disabled has no next run)", next(x for x in cl.req("GET", f"/api/apps/{web}/jobs").json() if x["id"] == slow["id"])["next_run"] is None)
    cl.req("DELETE", f"/api/apps/{web}/jobs/{slow['id']}")
    check("job deleted", slow["id"] not in [x["id"] for x in cl.req("GET", f"/api/apps/{web}/jobs").json()])
    cl.req("POST", "/api/users", json={"username": "viewer1", "password": "hunter2hunter2", "role": "viewer"})
    vw = login_as(base, "viewer1", "hunter2hunter2")
    vj = vw.req("GET", f"/api/apps/{web}/jobs").json()
    check("a viewer sees jobs but not their commands", len(vj) >= 1 and all(x["command"] == "" and x["last_output"] == "" for x in vj))
    check("a viewer cannot create or run jobs", vw.req("POST", f"/api/apps/{web}/jobs", json={"name": "x", "schedule": "* * * * *", "command": "id"}).status_code == 403
          and vw.req("POST", f"/api/apps/{web}/jobs/{cron_job['id']}/run").status_code == 403)

    # ── set up: scheduled backup and off-site copy ──────────────────────────
    check("backup schedule validated", cl.req("PUT", "/api/system/backup/schedule", json={"schedule": "nope", "keep": 3}).status_code == 400
          and cl.req("PUT", "/api/system/backup/schedule", json={"schedule": "* * * * *", "keep": 0}).status_code == 400)
    dav = Sink()
    r = cl.req("PUT", "/api/system/backup/remote", json={"kind": "webdav", "url": dav.url + "/dav", "username": "u", "password": "p4ss"})
    check("webdav remote saved, password masked", r.status_code == 200 and r.json()["remote"]["password"] == "***" and "p4ss" not in r.text)
    check("remote needs its fields", cl.req("PUT", "/api/system/backup/remote", json={"kind": "s3", "bucket": "b"}).status_code == 400
          and cl.req("PUT", "/api/system/backup/remote", json={"kind": "scp", "host": "h", "user": "u", "path": "/p", "private_key": "nope"}).status_code == 400
          and cl.req("PUT", "/api/system/backup/remote", json={"kind": "ftp"}).status_code == 400)
    cl.req("PUT", "/api/system/backup/remote", json={"kind": "webdav", "url": dav.url + "/dav", "username": "u", "password": "***"})
    t = cl.req("POST", "/api/system/backup/remote/test").json()
    check("remote test uploads a file", t["ok"] and any(g["path"] == "/dav/venvy-test.txt" for g in dav.got), str(t))
    auth = dav.got[0]["headers"].get("authorization", "")
    check("... with the stored password after ***", base64.b64decode(auth.split()[1]).decode() == "u:p4ss", auth)
    check("remote test fails clearly", not httpx.Client(base_url=base, headers={"x-venvy-key": cl.key}, cookies=cl.c.cookies).post("/api/system/backup/remote/test", json={"kind": "webdav", "url": "http://127.0.0.1:9/none"}, headers={"x-csrf-token": cl.c.cookies.get("csrf_token") or "", "x-venvy-key": cl.key}).json()["ok"])
    r = cl.req("PUT", "/api/system/backup/schedule", json={"schedule": "* * * * *", "keep": 1})
    check("schedule saved with its next run", r.status_code == 200 and r.json()["next_run"] and r.json()["keep"] == 1, r.text)

    # ── the one wait ────────────────────────────────────────────────────────
    time.sleep(max(0, 20 - (time.time() - t_start)))
    h = cl.status(web)["health"]
    check("health: healthy after the grace period", h and h["status"] == "healthy" and h["code"] == 200, str(h))
    cl.req("POST", f"/api/apps/{web}/files/op", json={"operation": "save", "path": "sick", "content": "x"})
    pid_before = cl.status(web)["pid"]
    ev = events("app_unhealthy", 40)
    check("health: failing checks notify", bool(ev) and "failing its health check" in ev[0]["title"] and "HTTP 500" in ev[0]["body"], str(ev))
    time.sleep(3)
    check("health: an unhealthy app is restarted", cl.status(web)["pid"] != pid_before or cl.status(web)["status"] in ("starting",), str(cl.status(web)["pid"]) + " vs " + str(pid_before))
    cl.req("POST", f"/api/apps/{web}/files/op", json={"operation": "delete", "path": "sick"})
    ev = events("app_recovered", 60)
    check("health: recovery notifies", bool(ev) and "healthy again" in ev[0]["title"], str(ev))
    check("health: unknown/healthy shown on the app", cl.status(web)["health"]["status"] in ("healthy", "unknown"))
    check("health: no check, no health field", cl.status(make_app(cl, name="Plain"))["health"] is None)

    hist = cl.req("GET", f"/api/apps/{web}/metrics/history", params={"minutes": 30}).json()["samples"]
    check("app metric history is recorded", len(hist) >= 3 and all({"ts", "cpu", "mem", "disk"} <= set(x) for x in hist) and hist[-1]["mem"] > 1_000_000 and hist[-1]["disk"] > 0, str(hist[-2:]))
    host = cl.req("GET", "/api/system/metrics/history", params={"minutes": 30}).json()["samples"]
    check("host metric history is recorded", len(host) >= 3 and 0 < host[-1]["disk"] <= 100 and 0 < host[-1]["mem"] <= 100, str(host[-2:]))
    check("history is bounded to the points asked", len(cl.req("GET", f"/api/apps/{web}/metrics/history", params={"minutes": 1}).json()["samples"]) <= 240)
    disk = cl.req("GET", f"/api/apps/{web}/disk", params={"refresh": "true"}).json()
    check("disk usage of an app", disk["bytes"] > 1_000_000, str(disk))

    ev = events("alert_fired", 70, has="memory above 1 MB")
    mem_alert = [e for e in ev if "memory above 1 MB" in e["title"]]
    check("alert fires after its duration", bool(mem_alert) and mem_alert[0]["level"] == "error", str(ev))
    check("... and is shown as firing", next(r for r in cl.req("GET", "/api/system/alerts").json() if r["id"] == rule["id"])["firing"] is True)
    check("... while the quiet one is not", next(r for r in cl.req("GET", "/api/system/alerts").json() if r["id"] == quiet["id"])["firing"] is False)
    import sqlite3
    con = sqlite3.connect(str(d / "data.db")); con.execute("UPDATE alertrule SET threshold=100000000 WHERE id=?", (rule["id"],)); con.commit(); con.close()
    ev = events("alert_cleared", 30, has="back under")
    check("alert clears when back under", bool(ev) and "back under" in ev[0]["title"], str(ev))

    cl.req("PUT", f"/api/apps/{web}", json={"disk_limit_mb": 1})
    ev = events("alert_fired", 30, has="disk limit")
    check("an app over its disk limit alerts", any("disk limit" in e["title"] for e in ev), str(ev))
    cl.req("PUT", f"/api/apps/{web}", json={"disk_limit_mb": 0})
    ev = events("alert_cleared", 30, has="under its disk limit")
    check("... and clears when the limit goes", any("disk limit" in e["title"] for e in ev), str(ev))
    check("disk limit validated", cl.req("PUT", f"/api/apps/{web}", json={"disk_limit_mb": -5}).status_code == 400)

    j = next(x for x in cl.req("GET", f"/api/apps/{web}/jobs").json() if x["id"] == cron_job["id"])
    check("scheduled job ran on its own", j["last_status"] == "ok" and "JOB-RAN hello shared-ok" in j["last_output"] and "/apps/" in j["last_output"], str(j))
    sch = cl.req("GET", "/api/system/backup/schedule").json()
    deadline = time.time() + 60
    while time.time() < deadline and not (sch["last"] and sch["last"].get("file")):
        time.sleep(2); sch = cl.req("GET", "/api/system/backup/schedule").json()
    check("scheduled backup ran", sch["last"] and sch["last"]["ok"] and sch["last"]["file"], str(sch))
    check("... and went off-site", sch["last"]["remote"].startswith("ok: ") and any(g["method"] == "PUT" and g["path"].startswith("/dav/full-") and g["body"][:2] == b"\x1f\x8b" for g in dav.got), str(sch["last"]))
    cl.req("PUT", "/api/system/backup/schedule", json={"schedule": "", "keep": 1})     # stop the every-minute schedule
    cl.req("POST", "/api/system/backup/schedule/run"); time.sleep(12)
    cl.req("POST", "/api/system/backup/schedule/run"); time.sleep(12)
    files = [b for b in cl.req("GET", "/api/system/backups").json() if b["filename"].startswith("full-")]
    check("retention keeps the newest scheduled backup only", len(files) == 1, str([f["filename"] for f in files]))
    ok = events("backup_ok", 5)
    check("scheduled backup notifies", bool(ok), "")

    # S3 signing end to end (path-style endpoint)
    s3 = Sink()
    r = cl.req("PUT", "/api/system/backup/remote", json={"kind": "s3", "endpoint": s3.url, "bucket": "bk", "prefix": "venvy/", "region": "eu-west-1", "access_key": "AKID", "secret_key": "SECRET"})
    check("s3 remote saved, secret masked", r.status_code == 200 and r.json()["remote"]["secret_key"] == "***" and "SECRET" not in r.text)
    t = cl.req("POST", "/api/system/backup/remote/test").json()
    g = s3.got[0] if s3.got else {"headers": {}, "path": "", "body": b""}
    check("s3 test uploads to bucket/prefix/name", t["ok"] and g["path"] == "/bk/venvy/venvy-test.txt", str(t) + g["path"])
    check("... signed with SigV4 over the real payload hash", g["headers"].get("authorization", "").startswith("AWS4-HMAC-SHA256 Credential=AKID/")
          and "/eu-west-1/s3/aws4_request" in g["headers"]["authorization"]
          and g["headers"].get("x-amz-content-sha256") == hashlib.sha256(g["body"]).hexdigest(), str(g["headers"]))
    name = files[0]["filename"]
    r = cl.req("POST", f"/api/system/backups/{name}/upload"); res = cl.job(r)
    check("an existing backup can be sent off-site", res["ok"] and any(x["path"] == f"/bk/venvy/{name}" for x in s3.got), str(res))
    check("off-site copy removed", cl.req("DELETE", "/api/system/backup/remote").status_code == 200 and cl.req("GET", "/api/system/backup/schedule").json()["remote"] is None)
    check("no remote: test and upload refused", cl.req("POST", "/api/system/backup/remote/test").status_code == 400 and cl.req("POST", f"/api/system/backups/{name}/upload").status_code == 400)

    # one real failing channel must not block the others
    sink.close(); dav.close(); s3.close()

print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
