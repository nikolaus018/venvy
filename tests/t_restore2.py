"""A corrupt archive must fail before anything is stopped; restore under concurrent DB traffic must yield a sound DB."""
import sqlite3, sys, threading, time
from pathlib import Path
import httpx
from harness import admin, make_app, server
repo = sys.argv[1]
with server(repo, 55003) as (base, data, srv):
    cl = admin(base)
    app_id = make_app(cl)
    cl.req("POST", f"/api/apps/{app_id}/control?action=start"); cl.wait_status(app_id, "running")
    bad = Path(data) / "backups"; bad.mkdir(exist_ok=True)
    (bad / "full-20200101-000000.tar.gz").write_bytes(b"not a tarball at all")
    try:
        cl.job(cl.req("POST", "/api/system/backup/restore/full-20200101-000000.tar.gz", json={"confirm": "RESTORE"})); err = ""
    except RuntimeError as e:
        err = str(e)
    st = cl.status(app_id)["status"]
    print("corrupt archive ->", err[:70], "| app still:", st)
    ok1 = "nothing was changed" in err and st == "running"
    cl.req("POST", f"/api/apps/{app_id}/control?action=stop"); cl.wait_status(app_id, "stopped")
    name = [b for b in (cl.job(cl.req("POST", "/api/system/backup")), cl.req("GET", "/api/system/backups").json())][1][0]["filename"]
    cl.req("POST", "/api/users", json={"username": "later", "password": "laterlater1"})
    stop = threading.Event()
    def hammer():   # writers + readers during the swap
        c2 = admin(base)
        while not stop.is_set():
            try:
                c2.req("PUT", "/api/system/settings/noise", json={"value": str(time.time())})
                c2.req("GET", "/api/apps")
            except Exception:
                pass
    ts = [threading.Thread(target=hammer) for _ in range(4)]; [t.start() for t in ts]
    cl.job(cl.req("POST", f"/api/system/backup/restore/{name}", json={"confirm": "RESTORE"}))
    time.sleep(4); stop.set(); [t.join() for t in ts]
    for _ in range(120):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200: break
        except Exception: pass
        time.sleep(0.5)
    users = [u["username"] for u in cl.req("GET", "/api/users").json()]
    integ = sqlite3.connect(Path(data) / "data.db").execute("pragma integrity_check").fetchone()[0]
    print("users after restore:", users, "| integrity:", integ)
    sys.exit(0 if ok1 and users == ["admin"] and integ == "ok" else 1)
