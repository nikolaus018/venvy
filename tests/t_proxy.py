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
with server(repo, 55004, env={"VENVY_PROXY_SECRET": "s3kr1t", "IP_ALLOWLIST": "127.0.0.1,203.0.113.7", "PORT": "55004", "HOST": "127.0.0.1"}) as (base, d, srv):
    cl = admin(base)
    # app env must not contain the secret
    app_id = make_app(cl, code="import os\nleaked = [k for k in ('VENVY_PROXY_SECRET', 'IP_ALLOWLIST', 'HOST', 'PORT') if k in os.environ]\nprint('HAS_SECRET=' + str(bool(leaked)) + ' ' + ','.join(leaked), flush=True)\nimport time; time.sleep(30)\n")
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
          "| rotating w/o secret locks at try", g.index(429) + 1 if 429 in g else None, "|", logs.strip().splitlines()[-1][-40:])
    ok &= (spoof, real_bad, real_ok) == (200, 403, 200) and 429 in g and g.index(429) + 1 <= 5 and "HAS_SECRET=False" in logs

# Who opened a loopback connection, from /proc/net/tcp as Linux writes it
# (no server needed): the client end of 127.0.0.1:40001 -> 127.0.0.1:54783
# belongs to uid 1001; its server end (Venvy's, uid 0) must not be the one found.
import os, tempfile
sys.path.insert(0, repo)
from venvy.auth import _proc_address, _socket_owner   # noqa: E402
d = tempfile.mkdtemp()
v4, v6 = os.path.join(d, "tcp"), os.path.join(d, "tcp6")
head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
with open(v4, "w") as f:
    f.write(head)
    f.write("   0: 0100007F:D5FF 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 11 1\n")
    f.write("   1: 0100007F:D5FF 0100007F:9C41 01 00000000:00000000 00:00000000 00000000     0        0 12 1\n")
    f.write("   2: 0100007F:9C41 0100007F:D5FF 01 00000000:00000000 00:00000000 00000000  1001        0 13 1\n")
with open(v6, "w") as f:
    f.write(head)
    # ::1 port 40002 -> ::1 port 54783, uid 1002; and a v4-mapped 127.0.0.1 port 40003, uid 1003.
    f.write("   0: 00000000000000000000000001000000:9C42 00000000000000000000000001000000:D5FF 01 0:0 0:0 0  1002 0 14 1\n")
    f.write("   1: 0000000000000000FFFF00000100007F:9C43 0000000000000000FFFF00000100007F:D5FF 01 0:0 0:0 0  1003 0 15 1\n")
found = (_proc_address("0100007F:D5FF"), _socket_owner("127.0.0.1", 40001, "127.0.0.1", 54783, (v4, v6)),
         _socket_owner("::1", 40002, "::1", 54783, (v4, v6)),
         _socket_owner("::ffff:127.0.0.1", 40003, "127.0.0.1", 54783, (v4, v6)),
         _socket_owner("127.0.0.1", 40009, "127.0.0.1", 54783, (v4, v6)))
print("socket owners from /proc/net/tcp:", found)
ok &= found == (("127.0.0.1", 54783), 1001, 1002, 1003, None)
sys.exit(0 if ok else 1)
