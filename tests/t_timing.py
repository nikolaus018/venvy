"""Login response time for unknown vs known username (wrong password)."""
import statistics, sys, time
from harness import admin, server
repo = sys.argv[1]
with server(repo, 54994, env={"TRUSTED_PROXY_IPS": "127.0.0.1"}) as (base, d, srv):
    cl = admin(base)
    cl.req("POST", "/api/auth/logout")
    def t(user, i):
        # vary CF-Connecting-IP (trusted from loopback) so the limiter doesn't lock us out
        h = {**cl._h("POST"), "cf-connecting-ip": f"10.0.{i % 250}.{i // 250 + 1}"}
        t0 = time.perf_counter()
        cl.c.post("/api/auth/login", json={"username": user, "password": "wrongpass123"}, headers=h)
        return time.perf_counter() - t0
    known = [t("admin", i) for i in range(15)]
    unknown = [t("ghost", 1000 + i) for i in range(15)]
    k, u = statistics.median(known) * 1000, statistics.median(unknown) * 1000
    print(f"median ms  known user: {k:.1f}   unknown user: {u:.1f}   ratio {k / u:.1f}x")
    sys.exit(0 if k / u < 1.5 else 1)
