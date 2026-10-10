"""Rotating a spoofed CF-Connecting-IP from loopback must not allow unlimited guesses; with VENVY_PROXY_SECRET the header needs the secret."""
import sys
from harness import admin, make_app, server, APP_CODE
repo = sys.argv[1]
def guesses(cl, n, extra=None):
    out = []
    for i in range(n):
        h = {**cl._h("POST"), "cf-connecting-ip": f"10.9.{i // 200}.{i % 200 + 1}", **(extra or {})}
        out.append(cl.c.post("/api/auth/login", json={"username": "admin", "password": f"wrongpass{i}x"}, headers=h).status_code)
    return out
ok = True
with server(repo, 55004) as (base, d, srv):
    cl = admin(base); cl.req("POST", "/api/auth/logout")
    g = guesses(cl, 30)
    print("no secret, rotating IPs: first 429 at try", g.index(429) + 1 if 429 in g else None)
    ok &= 429 in g and g.index(429) + 1 <= 20
with server(repo, 55004, env={"VENVY_PROXY_SECRET": "s3kr1t", "IP_ALLOWLIST": "127.0.0.1,203.0.113.7"}) as (base, d, srv):
    cl = admin(base)
    # app env must not contain the secret
    app_id = make_app(cl, code="import os\nprint('HAS_SECRET=' + str('VENVY_PROXY_SECRET' in os.environ), flush=True)\nimport time; time.sleep(30)\n")
    cl.req("POST", f"/api/apps/{app_id}/control?action=start"); cl.wait_status(app_id, "running")
    import time; time.sleep(1.5)
    logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
    cl.req("POST", f"/api/apps/{app_id}/control?action=stop")
    spoof = cl.c.get("/api/auth/status", headers={"cf-connecting-ip": "198.51.100.1"}).status_code                       # header ignored -> loopback, allowed
    real_bad = cl.c.get("/api/auth/status", headers={"cf-connecting-ip": "198.51.100.1", "x-venvy-proxy-secret": "s3kr1t"}).status_code  # believed -> not allowlisted
    real_ok = cl.c.get("/api/auth/status", headers={"cf-connecting-ip": "203.0.113.7", "x-venvy-proxy-secret": "s3kr1t"}).status_code
    cl.req("POST", "/api/auth/logout")
    g = guesses(cl, 8)
    print("secret set: header w/o secret ignored:", spoof == 200, "| with secret, non-allowlisted:", real_bad, "allowlisted:", real_ok,
          "| rotating w/o secret locks at try", g.index(429) + 1 if 429 in g else None, "|", logs.strip().splitlines()[-1][-16:])
    ok &= (spoof, real_bad, real_ok) == (200, 403, 200) and 429 in g and g.index(429) + 1 <= 5 and "HAS_SECRET=False" in logs
sys.exit(0 if ok else 1)
