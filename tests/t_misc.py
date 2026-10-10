"""Assorted regressions. usage: t_misc.py <repo> <case>"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import httpx

from harness import Client, admin, make_app, server

repo, case = sys.argv[1], sys.argv[2]
PORT = int(os.environ.get("T_PORT", "54993"))


def done(ok, msg=""):
    print(("PASS " if ok else "FAIL ") + case, msg)
    sys.exit(0 if ok else 1)


if case == "stuck_status":
    data = tempfile.mkdtemp(prefix="venvy-t-")
    with server(repo, PORT, data=data, keep=True) as (base, d, srv):
        cl = admin(base)
        app_id = make_app(cl)
    con = sqlite3.connect(Path(data) / "data.db")
    con.execute("update app set status='stopping'")
    con.commit(); con.close()
    with server(repo, PORT, data=data) as (base, d, srv):
        cl = admin(base)
        st = cl.status(app_id)["status"]
        done(st == "stopped", f"status after reboot: {st}")

if case == "login_lockout":
    # 4 wrong passwords, wait past the limiter's 120 s sweep, 4 more:
    # the 5th failure overall must still lock.
    with server(repo, PORT) as (base, d, srv):
        cl = admin(base)
        cl.req("POST", "/api/auth/logout")
        codes = []
        for i in range(4):
            codes.append(cl.req("POST", "/api/auth/login", json={"username": "admin", "password": f"wrongpass{i}9"}).status_code)
        time.sleep(float(os.environ.get("SWEEP_WAIT", "125")))
        codes.append(cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "wrongpassX9"}).status_code)
        done(codes == [401, 401, 401, 401, 429], str(codes))

if case == "totp_nonascii":
    with server(repo, PORT) as (base, d, srv):
        cl = admin(base)
        cl.req("GET", "/api/auth/totp/setup")
        r = cl.req("POST", "/api/auth/totp/setup/confirm", json={"totp_code": "１２３４５６"})
        done(r.status_code == 400, f"status {r.status_code}")

if case == "settings_secret":
    with server(repo, PORT) as (base, d, srv):
        cl = admin(base)
        r = cl.req("POST", "/api/system/settings/batch", json=[{"key": "gh_token", "value": "ghp_plaintext_token"}])
        con = sqlite3.connect(Path(d) / "data.db")
        raw = con.execute("select value from systemsetting where key='gh_token'").fetchone()[0]
        con.close()
        done(r.status_code == 200 and raw.startswith("enc:v1:"), raw[:20])

if case == "terminal_db":
    # An open terminal must not pin a DB connection / read transaction.
    import asyncio
    import websockets

    async def run():
        with server(repo, PORT) as (base, d, srv):
            cl = admin(base)
            app_id = make_app(cl)
            cookie = "; ".join(f"{k}={v}" for k, v in cl.c.cookies.items())
            socks = []
            for _ in range(int(os.environ.get('NTERM', '20'))):
                ws = await websockets.connect(
                    f"ws://127.0.0.1:{PORT}/api/apps/{app_id}/terminal",
                    additional_headers={"Cookie": cookie, "Origin": base})
                await ws.send("\x01" + cl.key)
                socks.append(ws)
            await asyncio.sleep(1.5)
            # echo through one of them to prove the PTY works
            await socks[0].send("echo term-$((40+2))\n")
            buf = ""
            end = time.time() + 5
            while time.time() < end and "term-42" not in buf:
                try:
                    buf += await asyncio.wait_for(socks[0].recv(), 1)
                except asyncio.TimeoutError:
                    pass
            t0 = time.time()
            try:
                r = await asyncio.to_thread(lambda: httpx.get(base + "/api/health", timeout=8))
                ok_health = r.status_code == 200
            except Exception as e:
                ok_health = False
            dt = time.time() - t0
            for ws in socks:
                await ws.close()
            await asyncio.sleep(float(os.environ.get("REAP_WAIT", "5")))
            import psutil
            kids = [c for c in psutil.Process(srv.pid).children(recursive=True) if "bash" in (c.name() or "")]
            for c in kids: print("  left:", c.pid, c.status(), c.cmdline()[:3], "ppid", c.ppid())
            zombies = [c for c in kids if c.status() == psutil.STATUS_ZOMBIE]
            if "term-42" not in buf: print("BUF:", repr(buf[-400:]))
            print(f"pty echo ok: {'term-42' in buf}; health with 20 terminals open: {ok_health} in {dt:.1f}s; "
                  f"shells left: {len(kids)} (zombies {len(zombies)})")
            return "term-42" in buf and ok_health and dt < 3 and not kids
    done(asyncio.run(run()))

if case == "key_perms":
    # The key must never exist with wider permissions, even under a lax umask.
    with server(repo, PORT, env={}) as (base, d, srv):
        pass
    data = tempfile.mkdtemp(prefix="venvy-t-")
    import subprocess
    code = (
        "import os, sys, threading; os.umask(0); sys.argv=['main.py'];"
        f"sys.path.insert(0, {repo!r});"
        "import importlib.util as u;"
        f"spec=u.spec_from_file_location('m', os.path.join({repo!r}, {os.environ.get('SMOKE_ENTRY', 'main.py')!r}));"
        "m=u.module_from_spec(spec); spec.loader.exec_module(m);"
        "c = getattr(m, '_crypto', m);"
        "out=[];"
        "ts=[threading.Thread(target=lambda: out.append(c._encrypt_secret('x'))) for _ in range(16)];"
        "[t.start() for t in ts]; [t.join() for t in ts];"
        "c._fernet_instance=None;"
        "print(oct(os.stat('.venvy_key').st_mode & 0o777), all(c._decrypt_secret(o)=='x' for o in out))"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=data, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr[-800:])
    done(r.stdout.strip() == "0o600 True")

raise SystemExit("unknown case")
