"""Audit trail, notifications, shared variables, env history, API tokens, per-app
permissions, log search and download, Python versions, duplicating an app, and
serving an app on its own domain (HTTP and WebSocket)."""
import hashlib, hmac, json, sqlite3, sys, time
import httpx
from harness import (Sink, SERVER_APP, WS_APP, admin, free_port, login_as, make_app, server)
repo = sys.argv[1]; fails = []
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

with server(repo, 55021, env={"VENVY_METRICS_FLUSH_SEC": "5"}) as (base, d, srv):
    cl = admin(base)
    sink = Sink()

    # ── audit trail ─────────────────────────────────────────────────────────
    a1 = make_app(cl, name="One")
    r = cl.req("GET", "/api/audit?limit=20").json()
    check("audit records app creation", any(e["action"] == "app_create" and "One" in e["detail"] for e in r["entries"]), str(r)[:300])
    check("audit entry has user and address", all(e["user"] and e["ip"] for e in r["entries"] if e["action"] == "app_create"))
    check("audit filter by action prefix", all(e["action"].startswith("app_") for e in cl.req("GET", "/api/audit?action=app_").json()["entries"]))
    check("audit search in detail", cl.req("GET", "/api/audit?q=One").json()["total"] >= 1 and cl.req("GET", "/api/audit?q=zzz-nothing").json()["total"] == 0)
    check("audit paging", len(cl.req("GET", "/api/audit?limit=1&offset=0").json()["entries"]) == 1)

    # ── notification channels ───────────────────────────────────────────────
    r = cl.req("POST", "/api/system/notify", json={"name": "hook", "kind": "webhook", "config": {"url": sink.url + "/hook", "secret": "s3cr3t"}, "events": "app_crashed,job_failed"})
    check("channel created", r.status_code == 200, r.text)
    ch = r.json()
    check("secret is masked", ch["config"]["secret"] == "***" and "s3cr3t" not in r.text)
    check("listing masks too", "s3cr3t" not in cl.req("GET", "/api/system/notify").text)
    check("bad kind refused", cl.req("POST", "/api/system/notify", json={"name": "x", "kind": "pigeon", "config": {}}).status_code == 400)
    check("missing field refused", cl.req("POST", "/api/system/notify", json={"name": "x", "kind": "telegram", "config": {"bot_token": "t"}}).status_code == 400)
    check("unknown event refused", cl.req("POST", "/api/system/notify", json={"name": "x", "kind": "webhook", "config": {"url": sink.url}, "events": "nope"}).status_code == 400)
    check("bad url refused", cl.req("POST", "/api/system/notify", json={"name": "x", "kind": "webhook", "config": {"url": "ftp://x"}}).status_code == 400)
    t = cl.req("POST", f"/api/system/notify/{ch['id']}/test").json()
    check("test notification delivered", t["ok"], str(t))
    got = sink.wait(lambda g: g["path"] == "/hook")
    check("webhook payload and signature", bool(got) and json.loads(got[0]["body"])["event"] == "test"
          and got[0]["headers"]["x-venvy-signature"] == "sha256=" + hmac.new(b"s3cr3t", got[0]["body"], hashlib.sha256).hexdigest())
    r = cl.req("PUT", f"/api/system/notify/{ch['id']}", json={"name": "hook2", "kind": "webhook", "config": {"url": sink.url + "/hook", "secret": "***"}, "events": "*"})
    sink.got.clear(); cl.req("POST", f"/api/system/notify/{ch['id']}/test")
    got = sink.wait(lambda g: g["path"] == "/hook")
    check("*** keeps the stored secret", bool(got) and got[0]["headers"]["x-venvy-signature"] == "sha256=" + hmac.new(b"s3cr3t", got[0]["body"], hashlib.sha256).hexdigest())
    sink2 = Sink(status=500)
    r = cl.req("POST", "/api/system/notify", json={"name": "bad", "kind": "slack", "config": {"url": sink2.url + "/slack"}}).json()
    t = cl.req("POST", f"/api/system/notify/{r['id']}/test").json()
    check("a failing channel reports it", not t["ok"] and "500" in t["status"], str(t))
    check("slack payload shape", json.loads(sink2.got[0]["body"]).get("text", "").startswith("*"))
    cl.req("DELETE", f"/api/system/notify/{r['id']}")
    r = cl.req("POST", "/api/system/notify", json={"name": "ntfy", "kind": "ntfy", "config": {"url": sink.url, "topic": "venvy-test"}}).json()
    sink.got.clear(); cl.req("POST", f"/api/system/notify/{r['id']}/test")
    check("ntfy posts to the topic", bool(sink.wait(lambda g: g["path"] == "/venvy-test")))
    cl.req("DELETE", f"/api/system/notify/{r['id']}")
    # a crashing app tells the channel (events are filtered to the channel's list)
    sink.got.clear()
    crash = make_app(cl, name="Crasher", code="import time\ntime.sleep(1)\nraise SystemExit(3)\n")
    cl.req("POST", f"/api/apps/{crash}/control?action=start")
    got = sink.wait(lambda g: b"app_crashed" in g["body"], timeout=40)
    check("a crash is notified", bool(got) and json.loads(got[0]["body"])["app"] == "Crasher", str(sink.got)[:300])

    # ── shared variables and env history ────────────────────────────────────
    check("shared env set", cl.req("PUT", "/api/system/shared-env", json={"vars": {"SHARED_A": "from-shared", "SHARED_B": "shared-b"}}).status_code == 200)
    check("bad shared name refused", cl.req("PUT", "/api/system/shared-env", json={"vars": {"a b": "x"}}).status_code == 400)
    check("shared env readable", cl.req("GET", "/api/system/shared-env").json()["vars"]["SHARED_A"] == "from-shared")
    check("shared env is not a generic setting", cl.req("GET", "/api/system/settings/global_env").json().get("value", "") == "" and cl.req("PUT", "/api/system/settings/global_env", json={"value": "x"}).status_code == 400)
    envapp = make_app(cl, name="Env", code='import os,time\nprint("ENV", os.environ.get("SHARED_A"), os.environ.get("SHARED_B"), os.environ.get("OWN"), flush=True)\ntime.sleep(60)\n')
    cl.req("PUT", f"/api/apps/{envapp}", json={"env_vars": {"SHARED_B": "app-wins", "OWN": "secretvalue1"}})
    cl.req("POST", f"/api/apps/{envapp}/control?action=start"); cl.wait_status(envapp, "running"); time.sleep(1.5)
    lg = cl.req("GET", f"/api/apps/{envapp}/logs").json()["content"]
    check("shared variables reach apps, app's own win", "ENV from-shared app-wins secretvalue1" in lg, lg[-200:])
    cl.req("POST", f"/api/apps/{envapp}/control?action=stop"); cl.wait_status(envapp, "stopped")
    cl.req("PUT", f"/api/apps/{envapp}", json={"env_vars": {"SHARED_B": "app-wins", "OWN": "changedvalue2", "NEW1": "x"}})
    cl.req("PUT", f"/api/apps/{envapp}", json={"env_vars": {"NEW1": "x"}})
    h = cl.req("GET", f"/api/apps/{envapp}/env-history")
    check("env history lists what changed", h.status_code == 200 and len(h.json()) == 3, h.text[:300])
    rows = h.json()
    check("... added/changed/removed names", rows[0]["removed"] == ["OWN", "SHARED_B"] and rows[1]["changed"] == ["OWN"] and rows[1]["added"] == ["NEW1"] and rows[2]["added"] == ["OWN", "SHARED_B"], str(rows))
    check("env history never holds values", "secretvalue1" not in h.text and "changedvalue2" not in h.text)

    # ── API tokens ──────────────────────────────────────────────────────────
    r = cl.req("POST", "/api/tokens", json={"name": "ci", "scope": "full"})
    check("token created, shown once", r.status_code == 200 and r.json()["token"].startswith("venvy_"), r.text)
    tok = r.json()["token"]; tok_id = r.json()["id"]
    check("listing never shows the token", tok not in cl.req("GET", "/api/tokens").text and "prefix" in cl.req("GET", "/api/tokens").json()[0])
    bearer = lambda t: httpx.Client(base_url=base, headers={"Authorization": f"Bearer {t}"}, timeout=30)
    tc = bearer(tok)
    check("token reads apps", tc.get("/api/apps").status_code == 200)
    check("token writes without CSRF", tc.post(f"/api/apps/{a1}/control?action=start").status_code == 200)
    cl.wait_status(a1, "running"); tc.post(f"/api/apps/{a1}/control?action=stop")
    check("a token cannot make tokens or change the password", tc.post("/api/tokens", json={"name": "x"}).status_code == 403
          and tc.put("/api/auth/password", json={"current_password": "a", "password": "b", "password_confirm": "b"}).status_code == 403)
    ro = cl.req("POST", "/api/tokens", json={"name": "ro", "scope": "read", "expires_days": 30}).json()
    rc = bearer(ro["token"])
    check("read-only token reads", rc.get("/api/apps").status_code == 200)
    check("read-only token cannot write", rc.post(f"/api/apps/{a1}/control?action=start").status_code == 403)
    check("bad token refused", bearer("venvy_nope").get("/api/apps").status_code == 401 and bearer("hello").get("/api/apps").status_code == 401)
    check("bad bearer does not fall back to the session cookie", httpx.get(base + "/api/apps", headers={"Authorization": "Bearer venvy_x", "x-venvy-key": cl.key}, cookies=cl.c.cookies).status_code == 401)
    con = sqlite3.connect(str(d / "data.db")); con.execute("UPDATE apitoken SET expires_at='2000-01-01 00:00:00' WHERE id=?", (ro["id"],)); con.commit(); con.close()
    check("expired token refused", rc.get("/api/apps").status_code == 401)
    cl.req("DELETE", f"/api/tokens/{tok_id}")
    check("revoked token refused", tc.get("/api/apps").status_code == 401)
    check("token use is audited by user", any(e["action"] == "token_create" for e in cl.req("GET", "/api/audit?action=token").json()["entries"]))

    # ── per-app permissions ─────────────────────────────────────────────────
    a2 = make_app(cl, name="Two")
    check("create restricted operator", cl.req("POST", "/api/users", json={"username": "ops2", "password": "hunter2hunter2", "role": "operator"}).status_code == 200)
    uid = next(u["id"] for u in cl.req("GET", "/api/users").json() if u["username"] == "ops2")
    check("new user sees all apps by default", next(u for u in cl.req("GET", "/api/users").json() if u["id"] == uid)["all_apps"] is True)
    check("grant one app", cl.req("PUT", f"/api/users/{uid}/access", json={"all_apps": False, "app_ids": [a1]}).status_code == 200)
    check("unknown app refused", cl.req("PUT", f"/api/users/{uid}/access", json={"all_apps": False, "app_ids": [9999]}).status_code == 400)
    admin_id = next(u["id"] for u in cl.req("GET", "/api/users").json() if u["username"] == "admin")
    check("admin cannot be restricted", cl.req("PUT", f"/api/users/{admin_id}/access", json={"all_apps": False, "app_ids": []}).status_code == 400)
    ops = login_as(base, "ops2", "hunter2hunter2")
    ids = [a["id"] for a in ops.req("GET", "/api/apps").json()]
    check("restricted user lists only granted apps", ids == [a1], str(ids))
    check("... reaches a granted app", ops.req("GET", f"/api/apps/{a1}/files/tree").status_code == 200)
    for path, m in [(f"/api/apps/{a2}/files/tree", "GET"), (f"/api/apps/{a2}/logs", "GET"), (f"/api/apps/{a2}/control?action=start", "POST"),
                    (f"/api/apps/{a2}/packages", "GET"), (f"/api/apps/{a2}/jobs", "GET"), (f"/api/apps/{a2}/deploy", "GET")]:
        check(f"... 404 for {m} {path.split('/api/apps/')[1][:22]}", ops.req(m, path).status_code == 404)
    check("... cannot read another app's download", ops.req("GET", f"/api/apps/{a2}/files/download?path=main.py").status_code == 404)
    check("... a made-up ?ticket= does not skip the check", ops.req("GET", f"/api/apps/{a2}/files/tree?ticket=bogus").status_code == 404)
    otkt = ops.req("POST", "/api/auth/download-ticket").json()["ticket"]
    check("... nor does a real ticket of theirs", ops.req("GET", f"/api/apps/{a2}/files/tree?ticket={otkt}").status_code == 404
          and ops.c.get(f"/api/apps/{a2}/logs/download?ticket={otkt}").status_code == 404)
    ports = ops.req("GET", "/api/system/ports").json()
    check("... does not see other apps in the port list", all(e.get("app_name") in (None, "One") for e in ports["listening"]))
    mine = ops.req("POST", "/api/apps/upload", data={"name": "Mine", "runtime": "python", "entrypoint": "main.py"}).json()
    ops.job(type("R", (), {"json": lambda s=None: mine, "status_code": 200})())
    check("what a restricted user creates, they can use", mine["app_id"] in [a["id"] for a in ops.req("GET", "/api/apps").json()])
    check("admin still sees everything", {a1, a2, mine["app_id"]} <= {a["id"] for a in cl.req("GET", "/api/apps").json()})
    tk = cl.req("POST", "/api/tokens", json={"name": "x"}).json()
    # a token made by a restricted user is restricted too
    otk = ops.req("POST", "/api/tokens", json={"name": "ops-ci"}).json()["token"]
    check("a restricted user's token is restricted", [a["id"] for a in bearer(otk).get("/api/apps").json()] == [a1, mine["app_id"]] or
          {a["id"] for a in bearer(otk).get("/api/apps").json()} == {a1, mine["app_id"]})
    check("... and cannot reach other apps", bearer(otk).get(f"/api/apps/{a2}/files/tree").status_code == 404)
    check("restore all apps", cl.req("PUT", f"/api/users/{uid}/access", json={"all_apps": True, "app_ids": []}).status_code == 200
          and a2 in [a["id"] for a in ops.req("GET", "/api/apps").json()])
    cl.req("DELETE", f"/api/tokens/{tk['id']}")

    # ── log search and download ─────────────────────────────────────────────
    big = ('print("NEEDLE-OLD first line", flush=True)\n'
           'import sys\nfor i in range(130000): print("filler line number", i, "padding padding padding")\n'
           'sys.stdout.flush()\nimport time; time.sleep(60)\n')
    lg_app = make_app(cl, name="Logs", code=big)
    cl.req("POST", f"/api/apps/{lg_app}/control?action=start"); cl.wait_status(lg_app, "running", 30); time.sleep(6)
    cl.req("POST", f"/api/apps/{lg_app}/control?action=stop"); cl.wait_status(lg_app, "stopped")
    cl.req("POST", f"/api/apps/{lg_app}/files/op", json={"operation": "save", "path": "main.py", "content": 'print("NEEDLE-NEW second run", flush=True)\nprint("Error: Boom", flush=True)\nimport time; time.sleep(60)\n'})
    cl.req("POST", f"/api/apps/{lg_app}/control?action=start"); cl.wait_status(lg_app, "running"); time.sleep(2)
    s = cl.req("GET", f"/api/apps/{lg_app}/logs/search", params={"q": "needle"}).json()
    check("search finds lines in rotated and current logs", ["NEEDLE-OLD" in m["text"] for m in s["matches"]] == [True, False] and "NEEDLE-NEW" in s["matches"][1]["text"] and s["files"] >= 2, str(s)[:300])
    check("... naming the file", s["matches"][0]["file"] != s["matches"][1]["file"] and s["matches"][1]["file"] == "app.log")
    check("case-sensitive search", len(cl.req("GET", f"/api/apps/{lg_app}/logs/search", params={"q": "needle", "case": "true"}).json()["matches"]) == 0)
    check("regex search", len(cl.req("GET", f"/api/apps/{lg_app}/logs/search", params={"q": r"NEEDLE-(OLD|NEW)", "regex": "true"}).json()["matches"]) == 2)
    ctx = cl.req("GET", f"/api/apps/{lg_app}/logs/search", params={"q": "Boom", "context": 1}).json()["matches"][0]
    check("context lines", "NEEDLE-NEW" in ctx["before"][0], str(ctx))
    check("limit", cl.req("GET", f"/api/apps/{lg_app}/logs/search", params={"q": "filler", "limit": 5}).json()["truncated"] is True)
    check("bad regex is a 400", cl.req("GET", f"/api/apps/{lg_app}/logs/search", params={"q": "(", "regex": "true"}).status_code == 400)
    bomb = "a" * 40 + "!"
    t0 = time.time()
    # a catastrophic pattern must not freeze the server: it is killed, and the API stays up
    cl.req("POST", f"/api/apps/{lg_app}/control?action=stop"); cl.wait_status(lg_app, "stopped")
    cl.req("POST", f"/api/apps/{lg_app}/files/op", json={"operation": "save", "path": "main.py", "content": f'print("{bomb}", flush=True)\nimport time; time.sleep(30)\n'})
    cl.req("POST", f"/api/apps/{lg_app}/control?action=start"); cl.wait_status(lg_app, "running"); time.sleep(1.5)
    r = httpx.get(base + f"/api/apps/{lg_app}/logs/search", params={"q": "(a+)+$", "regex": "true"}, headers={"x-venvy-key": cl.key}, cookies=cl.c.cookies, timeout=40)
    check("a catastrophic regex times out instead of hanging Venvy", r.status_code in (408, 200) and time.time() - t0 < 60, str(r.status_code))
    check("... and Venvy still answers", httpx.get(base + "/api/health", timeout=5).status_code == 200)
    dl = cl.req("GET", f"/api/apps/{lg_app}/logs/download")
    check("download has all logs", dl.status_code == 200 and "NEEDLE-OLD" in dl.text and "attachment" in dl.headers["content-disposition"])
    tkt = cl.req("POST", "/api/auth/download-ticket").json()["ticket"]
    check("download by ticket", cl.c.get(f"/api/apps/{lg_app}/logs/download?ticket={tkt}").status_code == 200)
    cl.req("POST", f"/api/apps/{lg_app}/control?action=stop")

    # ── Python versions ─────────────────────────────────────────────────────
    py = next(r for r in cl.req("GET", "/api/runtimes").json() if r["key"] == "python")
    check("runtimes list Python versions", isinstance(py["versions"], list))
    check("unknown Python version refused", cl.req("POST", "/api/apps/upload", data={"name": "V", "runtime": "python", "python_version": "3.99"}).status_code == 400)
    if py["versions"]:
        v = py["versions"][0]
        r = cl.req("POST", "/api/apps/upload", data={"name": "Versioned", "runtime": "python", "entrypoint": "main.py", "python_version": v})
        vid = r.json()["app_id"]; cl.job(r)
        check("venv built with the chosen version", cl.status(vid)["runtime_version"] == v)
        rr = cl.req("POST", f"/api/apps/{vid}/venv/rebuild", json={"version": ""}); res = cl.job(rr)
        check("venv rebuilt back to the default", cl.status(vid)["runtime_version"] in (None, ""), str(res))
    else:
        check("only the default Python here", True)
    node_like = make_app(cl, name="NP"); cl.req("PUT", f"/api/apps/{node_like}", json={"runtime": "static"})
    check("rebuild refused for non-Python apps", cl.req("POST", f"/api/apps/{node_like}/venv/rebuild", json={"version": ""}).status_code == 400)

    # ── duplicate ───────────────────────────────────────────────────────────
    src = make_app(cl, name="Original", code='import time\nprint("DUP-OK", flush=True)\ntime.sleep(60)\n')
    cl.req("POST", f"/api/apps/{src}/files/op", json={"operation": "save", "path": "extra.txt", "content": "payload"})
    cl.req("PUT", f"/api/apps/{src}", json={"env_vars": {"KEEP": "yes"}, "memory_limit_mb": 256})
    r = cl.req("POST", f"/api/apps/{src}/duplicate", json={"name": "Copy of Original"})
    new_id = r.json()["app_id"]; res = cl.job(r)
    check("duplicate is a new app", new_id != src and res["name"] == "Copy of Original" and res["status"] == "stopped", str(res)[:200])
    check("duplicate has the files", "extra.txt" in [f["name"] for f in cl.req("GET", f"/api/apps/{new_id}/files/tree").json()])
    check("duplicate has settings and variables", json.loads(cl.status(new_id)["env_json"]) == {"KEEP": "yes"} and cl.status(new_id)["memory_limit_mb"] == 256)
    check("duplicate does not share the port or domain", cl.status(new_id)["port"] is None and cl.status(new_id)["domain"] is None)
    check("duplicate has its own environment", cl.status(new_id)["venv_path"] != cl.status(src)["venv_path"])
    cl.req("POST", f"/api/apps/{new_id}/control?action=start"); check("duplicate runs", cl.wait_status(new_id, "running"))
    time.sleep(1.5); check("... its own code", "DUP-OK" in cl.req("GET", f"/api/apps/{new_id}/logs").json()["content"])
    cl.req("POST", f"/api/apps/{new_id}/control?action=stop"); cl.wait_status(new_id, "stopped")
    check("deleting the original leaves the copy alone", cl.req("DELETE", f"/api/apps/{src}").status_code == 200 and cl.status(new_id)["name"] == "Copy of Original")

    # ── an app on its own domain ────────────────────────────────────────────
    port = free_port()
    web = make_app(cl, name="Web", code=SERVER_APP, manual_ports=[port], env_vars={"MARK": "m1"})
    dom = "web.example.test"
    check("domain is validated", cl.req("PUT", f"/api/apps/{web}", json={"domain": "not a host"}).status_code == 400 and cl.req("PUT", f"/api/apps/{web}", json={"domain": "localhost"}).status_code == 400)
    check("domain set", cl.req("PUT", f"/api/apps/{web}", json={"domain": dom.upper()}).json()["domain"] == dom)
    r = cl.c.request("PUT", f"/api/apps/{web}", headers={**cl._h("PUT"), "Host": "self.example.test"}, json={"domain": "self.example.test"})
    check("an app cannot take over the address Venvy is reached on", r.status_code == 400 and "Venvy itself" in r.text, r.text)
    other = make_app(cl, name="Other")
    check("a domain belongs to one app", cl.req("PUT", f"/api/apps/{other}", json={"domain": dom}).status_code == 409)
    hc = lambda path="/", **kw: httpx.request(kw.pop("method", "GET"), base + path, headers={"Host": dom}, timeout=10, **kw)
    check("stopped app: 503, not the Venvy UI", hc().status_code == 503 and "not running" in hc().text)
    cl.req("POST", f"/api/apps/{web}/control?action=start"); cl.wait_status(web, "running"); time.sleep(2)
    r = hc("/hello?x=1")
    check("app is served on its domain", r.status_code == 200 and r.text == "APP-OK m1 /hello?x=1", r.text)
    check("domain with a port in Host", httpx.get(base + "/", headers={"Host": f"{dom}:80"}, timeout=10).text.startswith("APP-OK"))
    r = hc("/post", method="POST", content=b"hello body")
    check("request bodies pass through", r.status_code == 201 and r.text == "POST:hello body", r.text)
    check("no login needed on the app's domain", "x-venvy" not in str(hc().headers).lower() and hc().status_code == 200)
    check("Venvy's own UI is not reachable under the app's name", "<title>Venvy" not in hc("/").text and hc("/api/apps").text.startswith("APP-OK"))
    check("other hosts still reach Venvy", httpx.get(base + "/api/health", timeout=5).status_code == 200 and "Venvy" in httpx.get(base + "/", headers={"Host": "venvy.example.test"}, timeout=5).text)
    check("domain removed", cl.req("PUT", f"/api/apps/{web}", json={"domain": ""}).json()["domain"] is None)
    time.sleep(0.2)
    check("... and then it is Venvy's again", "Venvy" in httpx.get(base + "/", headers={"Host": dom}, timeout=5).text)
    cl.req("PUT", f"/api/apps/{web}", json={"domain": dom})
    cl.req("POST", f"/api/apps/{web}/control?action=stop"); cl.wait_status(web, "stopped")

    wsport = free_port()
    wsa = make_app(cl, name="WS", code=WS_APP, manual_ports=[wsport])
    cl.req("PUT", f"/api/apps/{wsa}", json={"domain": "ws.example.test"})
    cl.req("POST", f"/api/apps/{wsa}/control?action=start"); cl.wait_status(wsa, "running"); time.sleep(1.5)
    try:
        import socket as _s
        from websockets.sync.client import connect
        port_v = int(base.rsplit(":", 1)[1])
        with connect(f"ws://ws.example.test:{port_v}/chat", sock=_s.create_connection(("127.0.0.1", port_v)), open_timeout=10) as ws:
            ws.send("ping 1"); a = ws.recv(timeout=10)
            ws.send(b"\x00\x01bin"); b = ws.recv(timeout=10)
        check("WebSocket is proxied (text and binary)", a == "ping 1" and b == b"\x00\x01bin", f"{a!r} {b!r}")
    except Exception as exc:
        check("WebSocket is proxied (text and binary)", False, repr(exc))
    cl.req("POST", f"/api/apps/{wsa}/control?action=stop")

    sink.close()

print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
