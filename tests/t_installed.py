"""Checks against an *installed* Venvy (systemd service), as set up by install.sh.

usage: t_installed.py <base-url> [--app-users] [--existing] [--ui-update VERSION]
  --app-users  expect per-app Unix users, systemd scopes and resource limits
  --existing   the admin account already exists (second run after an update)
  --ui-update  update from the UI's endpoint and expect VERSION to be running after

Run as root on the machine itself: it looks at processes and file owners.
"""
import asyncio
import os
import pwd
import subprocess
import sys
import time

import psutil
import websockets

from harness import Client

base = sys.argv[1]
APP_USERS = "--app-users" in sys.argv
EXISTING = "--existing" in sys.argv
UI_UPDATE = sys.argv[sys.argv.index("--ui-update") + 1] if "--ui-update" in sys.argv else None
fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name, extra if not cond else "")
    if not cond:
        fails.append(name)


APP = (
    "import os, time, getpass\n"
    "print('WHOAMI=' + getpass.getuser(), flush=True)\n"
    "print('PORT_SEEN=' + os.environ.get('PORT', 'unset'), flush=True)\n"
    "try:\n"
    "    open('/var/lib/venvy/data.db', 'rb').read(16); print('DB_READABLE=yes', flush=True)\n"
    "except Exception as e:\n"
    "    print('DB_READABLE=no', flush=True)\n"
    "while True: time.sleep(0.5)\n"
)

cl = Client(base)
cl.c.get("/")
if EXISTING:
    r = cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"})
    check("existing admin can still log in (data survived)", r.status_code == 200, r.text)
else:
    code = open("/var/lib/venvy/.setup-code").read().strip()
    r = cl.req("POST", "/api/auth/setup", json={"username": "admin", "password": "hunter2hunter2", "setup_code": code})
    check("first-run setup", r.status_code == 200, r.text)
cl.key = r.json().get("auth_key")

if UI_UPDATE:
    import json as _json
    info = cl.req("GET", "/api/system/update?refresh=true").json()
    check("update: supported on an installed Venvy", info.get("supported") is True, str(info))
    check("update: the newer release is seen", info.get("latest") == UI_UPDATE and info.get("update_available") is True, str(info))
    notes = cl.req("GET", "/api/system/update/notes?refresh=true").json()
    check("update: what's new lists only the newer release", notes.get("newer") is True and [r["version"] for r in notes.get("releases", [])] == [UI_UPDATE]
          and notes["releases"][0]["items"] == ["Second CI release for the update test."], str(notes))
    check("update: the update units are installed", subprocess.run(["systemctl", "is-active", "--quiet", "venvy-update.path"]).returncode == 0)
    st = os.stat("/opt/venvy/update")
    check("update: its status directory is root's", st.st_uid == 0 and (st.st_mode & 0o777) == 0o750, oct(st.st_mode))
    r = cl.req("POST", "/api/system/update")
    check("update: accepted", r.status_code == 200, r.text)
    check("update: a second one is refused while it runs", cl.req("POST", "/api/system/update").status_code == 409)
    # Venvy goes away while the installer swaps it, then comes back as the new version.
    end, done = time.time() + 600, None
    while time.time() < end:
        time.sleep(3)
        try:
            with open("/opt/venvy/update/status.json") as f:
                done = _json.load(f)
        except (OSError, ValueError):
            continue
        if done.get("state") in ("ok", "failed"):
            break
    with open("/opt/venvy/update/update.log", errors="replace") as f:
        log_tail = f.read()[-1500:]
    check("update: finished ok", bool(done) and done.get("state") == "ok", str(done) + "\n" + log_tail)
    for _ in range(60):
        try:
            if cl.c.get("/api/health").status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    r = cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"})
    cl.key = r.json().get("auth_key")
    info = cl.req("GET", "/api/system/update").json()
    check("update: the new version is running", info.get("current") == UI_UPDATE and info.get("state") == "ok", str(info))
    check("update: the request file is gone", not os.path.exists("/var/lib/venvy/.update-request"))
    print("FAILED" if fails else "ALL OK", fails)
    sys.exit(1 if fails else 0)

unit_user = subprocess.run(["systemctl", "show", "-p", "User", "--value", "venvy.service"], capture_output=True, text=True).stdout.strip()
check("service is active", subprocess.run(["systemctl", "is-active", "--quiet", "venvy.service"]).returncode == 0)
check("service enabled at boot", subprocess.run(["systemctl", "is-enabled", "--quiet", "venvy.service"]).returncode == 0)
check("service user", unit_user == ("root" if APP_USERS else "venvy"), unit_user)
check("env file is root-only", (os.stat("/etc/venvy/venvy.env").st_mode & 0o777) == 0o600)
check("page and static files served", cl.c.get("/").status_code == 200 and cl.c.get("/static/js/api.js").status_code == 200
      and cl.c.get("/static/css/tailwind.css").status_code == 200 and cl.c.get("/sw.js").status_code == 200)

if not EXISTING:
    r = cl.req("POST", "/api/apps/upload", data={"name": "Installed App", "runtime": "python", "entrypoint": "main.py"})
    check("create app accepted", r.status_code == 200, r.text)
    app_id = r.json()["app_id"]
    try:
        cl.job(r)
        created = True
    except Exception as e:
        created = False
        print("     create failed:", e)
    check("app created (venv built on this host)", created)
    r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "main.py", "content": APP})
    check("save file", r.status_code == 200, r.text)
    st = cl.status(app_id)
    check("new app starts without resource limits", st["memory_limit_mb"] is None and st["cpu_limit_percent"] is None, str(st))
    r = cl.req("PUT", f"/api/apps/{app_id}", json={"memory_limit_mb": 512})
    check("set a memory limit", r.status_code == 200, r.text)
    cl.req("POST", f"/api/apps/{app_id}/control?action=start")
    check("app starts", cl.wait_status(app_id, "running", 30))
    time.sleep(2.5)
    st = cl.status(app_id)
    logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
    expect_user = f"venvy-app-{app_id}" if APP_USERS else "venvy"
    check("app runs as the expected user", f"WHOAMI={expect_user}" in logs, logs[-300:])
    check("app does not inherit Venvy's PORT", "PORT_SEEN=unset" in logs, logs[-300:])
    proc_user = psutil.Process(st["pid"]).username() if st.get("pid") else None
    app_dir = st["app_dir"]
    owner = pwd.getpwuid(os.stat(os.path.join(app_dir, "main.py")).st_uid).pw_name
    check("saved file owned by the app's user", owner == expect_user, owner)
    check("limits flag matches the mode", st["resource_limits_enforced"] is APP_USERS, str(st["resource_limits_enforced"]))

    if APP_USERS:
        check("app cannot read Venvy's database", "DB_READABLE=no" in logs, logs[-300:])
        check("process owner (psutil)", proc_user == expect_user, str(proc_user))
        scope = subprocess.run(["systemctl", "is-active", f"venvy-app-{app_id}.scope"], capture_output=True, text=True).stdout.strip()
        check("app runs in its systemd scope", scope == "active", scope)
        mem = subprocess.run(["systemctl", "show", "-p", "MemoryMax", "--value", f"venvy-app-{app_id}.scope"], capture_output=True, text=True).stdout.strip()
        check("memory limit applied to the scope", mem == str(512 * 1024 * 1024), mem)
        check("app dir is private to its user", (os.stat(app_dir).st_mode & 0o777) == 0o700 and pwd.getpwuid(os.stat(app_dir).st_uid).pw_name == expect_user)
        # root must not follow an app-planted symlink out of the app directory
        os.symlink("/etc", os.path.join(app_dir, "escape"))
        r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "escape/venvy-pwned", "content": "x"})
        check("write through an app-planted symlink refused", r.status_code == 403 and not os.path.exists("/etc/venvy-pwned"), r.text)

    async def term():
        cookie = "; ".join(f"{k}={v}" for k, v in cl.c.cookies.items())
        ws = await websockets.connect(base.replace("http", "ws") + f"/api/apps/{app_id}/terminal", additional_headers={"Cookie": cookie, "Origin": base})
        await ws.send("\x01" + cl.key)
        await asyncio.sleep(2)
        await ws.send("echo me=$(id -un) cwd=$(pwd)\n")
        buf = ""
        end = time.time() + 6
        while time.time() < end and "me=" + expect_user not in buf:
            try:
                buf += await asyncio.wait_for(ws.recv(), 1)
            except asyncio.TimeoutError:
                pass
        await ws.close()
        return buf
    out = asyncio.run(term())
    check("terminal runs as the expected user in the app dir", f"me={expect_user}" in out and f"cwd={app_dir}" in out, repr(out[-300:]))

    b = cl.job(cl.req("POST", "/api/system/backup"), timeout=300)
    check("backup works", b.get("ok") is True)
    cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
    check("app stops", cl.wait_status(app_id, "stopped", 30))
    if APP_USERS:
        time.sleep(1)
        scope = subprocess.run(["systemctl", "is-active", f"venvy-app-{app_id}.scope"], capture_output=True, text=True).stdout.strip()
        check("scope is gone after stop", scope != "active", scope)
        # A deploy key made before per-app users were switched on belongs to the old
        # service user; a deploy must hand it to the app's user, who runs ssh.
        key = cl.req("POST", f"/api/apps/{app_id}/git/key")
        check("deploy key generated", key.status_code == 200, key.text)
        key_dir = f"/var/lib/venvy/deploy-keys/{app_id}"
        for p in (key_dir, f"{key_dir}/id_ed25519", f"{key_dir}/id_ed25519.pub"):
            os.chown(p, 0, 0)
        cl.req("PUT", f"/api/apps/{app_id}/git", json={"build_command": "true"})
        cl.req("POST", f"/api/apps/{app_id}/deploy", json={})
        end = time.time() + 60
        while time.time() < end and cl.req("GET", f"/api/apps/{app_id}/deploy").json()["state"]["running"]:
            time.sleep(0.5)
        owner = pwd.getpwuid(os.stat(f"{key_dir}/id_ed25519").st_uid).pw_name
        check("a deploy gives an old deploy key to the app's user", owner == expect_user, owner)
else:
    apps = cl.req("GET", "/api/apps").json()
    check("app list survived the update", len(apps) == 1 and apps[0]["name"] == "Installed App", str([a["name"] for a in apps]))

print("FAILED" if fails else "ALL OK", fails)
sys.exit(1 if fails else 0)
