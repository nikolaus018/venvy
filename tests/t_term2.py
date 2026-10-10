"""An open terminal must close once its session is gone."""
import asyncio, sys, time
import websockets
from harness import Client, admin, make_app, server
PORT = 55006
async def run(repo):
    with server(repo, PORT, env={"VENVY_TERMINAL_RECHECK_SEC": "1"}) as (base, d, srv):
        cl = admin(base); app_id = make_app(cl)
        cookie = "; ".join(f"{k}={v}" for k, v in cl.c.cookies.items())
        ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/api/apps/{app_id}/terminal", additional_headers={"Cookie": cookie, "Origin": base})
        await ws.send("\x01" + cl.key)
        await asyncio.sleep(2.5)
        alive_before = ws.state.name == "OPEN"
        await ws.send("echo still-$((1+1))\n")
        buf = ""
        end = time.time() + 4
        while time.time() < end and "still-2" not in buf:
            try: buf += await asyncio.wait_for(ws.recv(), 1)
            except asyncio.TimeoutError: pass
        await asyncio.to_thread(cl.req, "POST", "/api/auth/logout")
        closed = False
        end = time.time() + 8
        while time.time() < end:
            try: await asyncio.wait_for(ws.recv(), 1)
            except asyncio.TimeoutError: continue
            except websockets.ConnectionClosed: closed = True; break
        print("open while signed in:", alive_before and "still-2" in buf, "| closed after logout:", closed)
        return alive_before and "still-2" in buf and closed
sys.exit(0 if asyncio.run(run(sys.argv[1])) else 1)
