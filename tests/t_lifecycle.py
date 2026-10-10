"""Lifecycle regressions. usage: t_lifecycle.py <repo> <case>"""
import concurrent.futures as cf
import sys
import time

from harness import admin, app_procs, make_app, server

repo, case = sys.argv[1], sys.argv[2]

CRASH = "import sys, time\nprint('boom', flush=True)\ntime.sleep(0.3)\nsys.exit(3)\n"

with server(repo, 54992) as (base, data, srv):
    cl = admin(base)

    if case == "double_start":
        app_id = make_app(cl)
        with cf.ThreadPoolExecutor(6) as ex:
            list(ex.map(lambda _: cl.req("POST", f"/api/apps/{app_id}/control?action=start").status_code, range(6)))
        cl.wait_status(app_id, "running")
        time.sleep(2)
        n = len(app_procs(data))
        print("processes after 6 concurrent starts:", n)
        cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
        cl.wait_status(app_id, "stopped")
        time.sleep(1.5)
        left = len(app_procs(data))
        print("processes left after stop:", left)
        sys.exit(0 if (n == 1 and left == 0) else 1)

    if case == "restart_storm":
        app_id = make_app(cl)
        cl.req("POST", f"/api/apps/{app_id}/control?action=start")
        cl.wait_status(app_id, "running")
        with cf.ThreadPoolExecutor(5) as ex:
            list(ex.map(lambda _: cl.req("POST", f"/api/apps/{app_id}/control?action=restart").status_code, range(5)))
        time.sleep(8)
        st = cl.status(app_id)
        n = len(app_procs(data))
        print("status:", st["status"], "pid:", st["pid"], "processes:", n)
        ok = st["status"] == "running" and n == 1 and st["pid"] in app_procs(data)
        cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
        cl.wait_status(app_id, "stopped")
        time.sleep(1.5)
        left = len(app_procs(data))
        print("processes left after stop:", left)
        sys.exit(0 if ok and left == 0 else 1)

    if case == "stop_crashloop":
        # An "always" app that keeps crashing must stay down once the user
        # presses Stop, even if the press lands between two restarts.
        app_id = make_app(cl, code=CRASH, restart_policy="always")
        cl.req("POST", f"/api/apps/{app_id}/control?action=start")
        time.sleep(2.0)   # it has crashed by now; supervisor is waiting to restart
        st = cl.status(app_id)
        print("before stop:", st["status"], "pid:", st["pid"])
        cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
        time.sleep(0.5)
        before = cl.status(app_id)["last_start_time"]
        time.sleep(14)
        after = cl.status(app_id)
        print("restarted after Stop:", after["last_start_time"] != before, "| status:", after["status"])
        sys.exit(0 if after["last_start_time"] == before else 1)

    if case == "shutdown_logs":
        app_id = make_app(cl)
        cl.req("POST", f"/api/apps/{app_id}/control?action=start")
        cl.wait_status(app_id, "running")
        time.sleep(1)
        cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
        cl.wait_status(app_id, "stopped")
        time.sleep(0.5)
        logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
        print("shutdown marker in logs:", "SHUTDOWN-MARKER" in logs)
        sys.exit(0 if "SHUTDOWN-MARKER" in logs else 1)

    raise SystemExit("unknown case")
