"""Regression tests for v4.9.5 security and reliability fixes:
- Domain port ownership check on PUT /api/apps/{id}
- Browser-session-only enforcement on user management and backup restore
- Viewer deploy-log redaction and WS terminal rejection
- Logout key verification and Bearer auth_status
- Non-admin own_hosts non-poisoning and 409 domain conflict non-disclosure
- Dangerous env var blocking across app env and shared env
- WS terminal Origin rejection before handshake accept
- Cron wrap-around weekday ranges & stepped starts, pre-release version order,
  cloud metadata SSRF blocking, static_server symlink containment, exec_env helper.
"""
import asyncio
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

from harness import Client, admin, free_port, login_as, make_app, server

repo = sys.argv[1]
PORT = 55029
fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "")
    if not cond:
        fails.append(name)


with server(repo, PORT) as (base, data, srv):
    cl = admin(base)

    # ── 1. Domain port ownership check (H-1) ────────────────────────────────
    app1 = make_app(cl, name="DomApp")
    r = cl.req("PUT", f"/api/apps/{app1}", json={"port": PORT, "domain": "venvyport.example.test"})
    check("domain cannot target Venvy's own port", r.status_code == 400 and "Venvy's own port" in r.text, f"{r.status_code} {r.text}")
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        fport = foreign.getsockname()[1]
        r = cl.req("PUT", f"/api/apps/{app1}", json={"port": fport, "domain": "foreign.example.test"})
        check("domain cannot target a port another process listens on", r.status_code == 409, f"{r.status_code} {r.text}")
    good_port = free_port()
    r = cl.req("PUT", f"/api/apps/{app1}", json={"port": good_port, "domain": "good.example.test"})
    check("domain on app's own free port succeeds", r.status_code == 200 and r.json()["domain"] == "good.example.test", f"{r.status_code} {r.text}")

    # ── 2. Browser-session-only enforcement (H-2, H-3) ──────────────────────
    tok = cl.req("POST", "/api/tokens", json={"name": "adm", "scope": "full"}).json()["token"]
    tc = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    check("auth/status recognizes Bearer token",
          tc.get("/api/auth/status").json().get("authenticated") is True)
    check("API token cannot create users",
          tc.post("/api/users", json={"username": "evil", "password": "password123", "role": "admin"}).status_code == 403)
    cl.req("POST", "/api/users", json={"username": "victim", "password": "password123", "role": "operator"})
    vid = next(u["id"] for u in cl.req("GET", "/api/users").json() if u["username"] == "victim")
    check("API token cannot change user access",
          tc.put(f"/api/users/{vid}/access", json={"all_apps": False, "app_ids": []}).status_code == 403)
    check("API token cannot delete users",
          tc.delete(f"/api/users/{vid}").status_code == 403)
    check("API token cannot restore backups",
          tc.post("/api/system/backup/restore/full-20260101-000000.tar.gz", json={"confirm": "RESTORE"}).status_code == 403)

    # ── 3. Restricted operator domain 409 non-disclosure (M-3) ──────────────
    app2 = make_app(cl, name="OpOwn")
    cl.req("PUT", f"/api/apps/{app1}", json={"name": "SecretCorpApp"})
    cl.req("PUT", f"/api/users/{vid}/access", json={"all_apps": False, "app_ids": [app2]})
    op_cl = login_as(base, "victim", "password123")
    r = op_cl.req("PUT", f"/api/apps/{app2}", json={"domain": "good.example.test"})
    check("409 domain conflict hides unpermitted app name",
          r.status_code == 409 and "SecretCorpApp" not in r.text, f"{r.status_code} {r.text}")

    # ── 4. Viewer deploy-log redaction (H-4) & non-admin own_hosts ──────────
    cl.req("POST", "/api/users", json={"username": "vwr", "password": "password123", "role": "viewer"})
    vw_raw = Client(base)
    vw_raw.c.get("/")
    r_vw_login = vw_raw.c.post(
        "/api/auth/login",
        headers={**vw_raw._h("POST"), "Host": "viewer-host.example.test"},
        json={"username": "vwr", "password": "password123"},
    )
    check("viewer login under custom Host succeeds", r_vw_login.status_code == 200)
    vw_raw.key = r_vw_login.json()["auth_key"]
    r_dom = cl.req("PUT", f"/api/apps/{app2}", json={"domain": "viewer-host.example.test"})
    check("viewer login does not poison own_hosts", r_dom.status_code == 200, f"{r_dom.status_code} {r_dom.text}")

    cl.req("PUT", f"/api/apps/{app1}/git", json={"build_command": "echo DEPLOY-BUILD-SECRET-LINE"})
    cl.req("POST", f"/api/apps/{app1}/deploy", json={"start": False})
    end = time.time() + 30
    while time.time() < end:
        st = cl.req("GET", f"/api/apps/{app1}/deploy").json()["state"]
        if not st["running"] and st["status"] not in (None, "running"):
            break
        time.sleep(0.3)
    adm_log = cl.req("GET", f"/api/apps/{app1}/deploy").json()["state"]["log"]
    vw_log = vw_raw.req("GET", f"/api/apps/{app1}/deploy").json()["state"]["log"]
    check("admin sees deploy log while viewer gets empty log",
          any("DEPLOY-BUILD-SECRET-LINE" in ln for ln in adm_log) and vw_log == [],
          f"adm={adm_log[-3:]} vw={vw_log}")

    # ── 5. Dangerous env var blocking (H-6) ─────────────────────────────────
    for bad_key in ("LD_PRELOAD", "PYTHONSTARTUP", "PYTHONPATH", "PYTHONHOME", "PYTHONINSPECT", "BASH_ENV", "OPENSSL_CONF", "DBUS_SESSION_BUS_ADDRESS"):
        r = cl.req("PUT", f"/api/apps/{app1}", json={"env_vars": {bad_key: "/tmp/evil"}})
        check(f"app env blocks {bad_key}", r.status_code == 400, f"{r.status_code} {r.text}")
    r = cl.req("PUT", "/api/system/shared-env", json={"vars": {"PYTHONINSPECT": "1"}})
    check("shared env blocks PYTHONINSPECT", r.status_code == 400, f"{r.status_code} {r.text}")

    # ── 6. Logout requires valid session key (M-4) ──────────────────────────
    s2 = admin(base)
    # Call logout with cookie only (missing X-Venvy-Key)
    r_bad_out = httpx.post(
        base + "/api/auth/logout",
        cookies=s2.c.cookies,
        headers={"x-csrf-token": s2.c.cookies.get("csrf_token") or ""},
    )
    check("logout without key still returns 200", r_bad_out.status_code == 200)
    check("session survives unauthenticated logout attempt",
          s2.req("GET", "/api/apps").status_code == 200)
    r_good_out = s2.req("POST", "/api/auth/logout")
    check("logout with valid key revokes session",
          r_good_out.status_code == 200 and s2.req("GET", "/api/apps").status_code == 401)

    # ── 7. WS terminal Origin + viewer rejection (M-1, M-2) ─────────────────
    async def check_ws():
        cookie = "; ".join(f"{k}={v}" for k, v in cl.c.cookies.items())
        bad_origin_rejected = False
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/api/apps/{app1}/terminal",
                additional_headers={"Cookie": cookie, "Origin": "https://evil.example"},
            ):
                pass
        except Exception:
            bad_origin_rejected = True

        vw_cookie = "; ".join(f"{k}={v}" for k, v in vw_raw.c.cookies.items())
        viewer_rejected = False
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/api/apps/{app1}/terminal",
                additional_headers={"Cookie": vw_cookie, "Origin": base},
            ) as ws:
                await ws.send("\x01" + vw_raw.key)
                try:
                    await asyncio.wait_for(ws.recv(), 3)
                except websockets.ConnectionClosed as cc:
                    viewer_rejected = cc.code == 1008
        except Exception:
            viewer_rejected = True
        return bad_origin_rejected, viewer_rejected

    bad_orig, vw_rej = asyncio.run(check_ws())
    check("WS terminal rejects cross-origin before accept", bad_orig)
    check("WS terminal rejects viewer role", vw_rej)

# ── 8. Unit checks (no server needed) ───────────────────────────────────────
sys.path.insert(0, os.path.abspath(repo))
from venvy import cron, updates, notify, remote_backup

# Cron wrap-around weekday range & stepped start & wildcard dedup
fields_wrap = cron.parse("0 9 * * fri-sun")
# cron weekdays: 0=Sun, 5=Fri, 6=Sat
check("cron fri-sun includes Fri, Sat, Sun", fields_wrap.dow == {0, 5, 6}, str(fields_wrap.dow))
fields_step = cron.parse("1/20 * * * *")
check("cron 1/20 starts at 1 (1, 21, 41)", fields_step.minutes == {1, 21, 41}, str(fields_step.minutes))
fields_star = cron.parse("1,* * * * *")
check("cron 1,* produces full minute set", len(fields_star.minutes) == 60)

# Updates pre-release ordering
check("updates: stable > rc > beta > older",
      updates.is_newer("v4.9.5", "v4.9.5-rc1")
      and updates.is_newer("v4.9.5-rc1", "v4.9.5-beta.2")
      and updates.is_newer("v4.9.5-beta.2", "v4.9.5-beta.1")
      and updates.is_newer("v4.9.5-beta.1", "v4.9.4")
      and not updates.is_newer("v4.9.5-rc1", "v4.9.5"))

# Cloud metadata SSRF checks in notify & remote_backup
for meta_url in (
    "http://169.254.169.254/latest/meta-data/",
    "http://100.100.100.200/latest/meta-data/",
    "http://[fd00:ec2::254]/latest/meta-data/",
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
):
    err = None
    try:
        notify.check_config("webhook", {"url": meta_url})
    except ValueError as e:
        err = str(e)
    check(f"notify blocks metadata URL {meta_url}", err is not None, str(err))
    err2 = None
    try:
        remote_backup.clean({"kind": "webdav", "url": meta_url, "username": "u", "password": "p"}, None)
    except ValueError as e:
        err2 = str(e)
    check(f"remote_backup blocks metadata URL {meta_url}", err2 is not None, str(err2))

# static_server symlink containment + // redirect check
work = Path(tempfile.mkdtemp(prefix="venvy-static-"))
try:
    pub = work / "pub"
    pub.mkdir()
    outside = work / "secret.txt"
    outside.write_text("TOP-SECRET-OUTSIDE")
    (pub / "index.html").write_text("<h1>hello</h1>")
    os.symlink(outside, pub / "escape.txt")
    st_port = free_port()
    p = subprocess.Popen(
        [sys.executable, str(Path(repo) / "venvy" / "runtime_assets" / "static_server.py"),
         "--port", str(st_port), "--dir", str(pub), "--root", str(pub)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        for _ in range(40):
            try:
                if httpx.get(f"http://127.0.0.1:{st_port}/", timeout=0.5).status_code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        r_ok = httpx.get(f"http://127.0.0.1:{st_port}/")
        r_esc = httpx.get(f"http://127.0.0.1:{st_port}/escape.txt")
        r_redir = httpx.get(f"http://127.0.0.1:{st_port}//evil.example", follow_redirects=False)
        check("static_server serves index.html", r_ok.status_code == 200 and "hello" in r_ok.text)
        check("static_server blocks symlink outside root",
              r_esc.status_code in (403, 404) and "TOP-SECRET-OUTSIDE" not in r_esc.text, f"{r_esc.status_code}")
        check("static_server blocks // open redirect",
              not (300 <= r_redir.status_code < 400 and r_redir.headers.get("location", "").startswith("//")),
              f"{r_redir.status_code} {r_redir.headers.get('location')}")
    finally:
        p.terminate()
        p.wait(5)

    # exec_env helper test (via unlinked inheritable fd, as isolation.py invokes it)
    from venvy.isolation import _prepare_exec_env
    tf, fd = _prepare_exec_env({"HELLO_FROM_HELPER": "yes-123", "PATH": os.environ.get("PATH", "/usr/bin:/bin")})
    try:
        res = subprocess.run(
            [sys.executable, "-I", str(Path(repo) / "venvy" / "runtime_assets" / "exec_env.py"),
             str(fd), sys.executable, "-c", "import os; print(os.environ.get('HELLO_FROM_HELPER'))"],
            pass_fds=(fd,),
            capture_output=True, text=True, check=True,
        )
    finally:
        tf.close()
    check("exec_env passes env via inherited fd", res.stdout.strip() == "yes-123", repr(res.stdout))
finally:
    shutil.rmtree(work, ignore_errors=True)

print("FAILED:" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
