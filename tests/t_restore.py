"""An 'always' + start_on_boot app that was running when the backup was taken
must come back from a restore as ONE supervised process with a working log pipe."""
import sys, time
import httpx
from harness import admin, app_procs, make_app, server

repo = sys.argv[1]
with server(repo, 54998) as (base, data, srv):
    cl = admin(base)
    app_id = make_app(cl, restart_policy="always", start_on_boot=True)
    cl.req("POST", f"/api/apps/{app_id}/control?action=start")
    cl.wait_status(app_id, "running")
    b = cl.job(cl.req("POST", "/api/system/backup"))
    name = cl.req("GET", "/api/system/backups").json()[0]["filename"]
    r = cl.req("POST", f"/api/system/backup/restore/{name}", json={"confirm": "RESTORE"})
    import os
    if not os.environ.get("SKIP_DOUBLE"):
        r2 = cl.req("POST", f"/api/system/backup/restore/{name}", json={"confirm": "RESTORE"})
        print("second concurrent restore:", r2.status_code)
    cl.job(r)
    # the window between the swap and the re-exec (3 s after the job is done).
    # Bounded by the clock, not by a sample count: listing processes is slow
    # on some hosts, and a late sample would see the app's proper boot start.
    mid = []
    window_end = time.time() + 2.5
    while time.time() < window_end:
        mid.append(len(app_procs(data)))
        time.sleep(0.25)
    time.sleep(3.5)
    for _ in range(120):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    time.sleep(4)
    st = cl.status(app_id)
    procs = app_procs(data)
    print("started during swap->exec window:", max(mid) > 0, mid)
    print("after restart: status", st["status"], "pid", st["pid"], "procs", procs)
    cl.req("DELETE", f"/api/apps/{app_id}/logs")
    cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
    cl.wait_status(app_id, "stopped")
    time.sleep(1)
    logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
    left = app_procs(data)
    print("shutdown output reached the log:", "SHUTDOWN-MARKER" in logs, "| left running:", left)
    ok = max(mid) == 0 and st["status"] == "running" and procs == [st["pid"]] and "SHUTDOWN-MARKER" in logs and not left
    sys.exit(0 if ok else 1)
