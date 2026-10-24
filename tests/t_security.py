"""Who may call what, swept over every route the server has.

- The session cookie alone gets nowhere: browsers send it to every hosted
  app on the same host, so every route a signed-in user can reach must also
  want the X-Venvy-Key header (kept in Venvy's own localStorage).
- A viewer is read-only, and admin routes are for admins: every route not
  listed below as open to that role must answer 403.
- No API map for strangers, HTTPS gets Secure cookies and HSTS, and the CSP
  does not let inline script run."""
import json, re, subprocess, sys, tempfile
import httpx
from harness import Client, admin, make_app, server
repo = sys.argv[1]; fails = []
PORT = 55031
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

# Answer without a session by design.
PUBLIC = {
    ("GET", "/api/health"), ("GET", "/api/auth/status"), ("POST", "/api/auth/setup"),
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"), ("POST", "/api/hooks/deploy/{app_id}"),
}
# Open to every signed-in role, viewers included.
ANY_ROLE = {
    ("GET", "/api/apps"), ("GET", "/api/apps/{app_id}/deploy"), ("GET", "/api/apps/{app_id}/disk"),
    ("GET", "/api/apps/{app_id}/jobs"), ("GET", "/api/apps/{app_id}/logs"),
    ("GET", "/api/apps/{app_id}/logs/download"), ("GET", "/api/apps/{app_id}/logs/search"),
    ("GET", "/api/apps/{app_id}/metrics/history"), ("GET", "/api/apps/{app_id}/packages"),
    ("GET", "/api/apps/{app_id}/releases"), ("GET", "/api/apps/{app_id}/scan-imports"),
    ("GET", "/api/apps/{app_id}/stats"), ("GET", "/api/apps/{app_id}/tunnel"),
    ("GET", "/api/jobs/{job_id}"), ("GET", "/api/runtimes"), ("GET", "/api/system/info"),
    # One's own account.
    ("GET", "/api/tokens"), ("POST", "/api/tokens"), ("DELETE", "/api/tokens/{token_id}"),
    ("GET", "/api/auth/totp/setup"), ("POST", "/api/auth/totp/setup/confirm"),
    ("POST", "/api/auth/totp/disable"), ("PUT", "/api/auth/password"), ("POST", "/api/auth/download-ticket"),
}
# Additionally open to operators.
OPERATOR = {
    ("GET", "/api/apps/{app_id}/env-history"), ("GET", "/api/apps/{app_id}/files/tree"),
    ("GET", "/api/apps/{app_id}/files/download"), ("GET", "/api/apps/{app_id}/tunnel/check"),
    ("GET", "/api/system/cloudflare/status"), ("GET", "/api/system/cloudflare/zones"),
    ("GET", "/api/system/ports"),
}

def routes():
    """Every (method, path) of the API. Listed in a child process in a scratch
    directory: Venvy keeps its data in the working directory."""
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from venvy.app import app; "
            "print(json.dumps([(m.upper(), p) for p, ops in app.openapi()['paths'].items() for m in ops]))")
    with tempfile.TemporaryDirectory() as cwd:
        out = subprocess.run([sys.executable, "-c", code, repo], cwd=cwd, capture_output=True, text=True, check=True)
    return [tuple(x) for x in json.loads(out.stdout.strip().splitlines()[-1])]

def fill(path, app_id):
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: str(app_id) if m.group(1) == "app_id" else "x", path)

def call(method, url, cookies, key, csrf):
    h = {"x-csrf-token": csrf}
    if key:
        h["x-venvy-key"] = key
    try:
        return httpx.request(method, url, cookies=cookies, headers=h, params={"path": "main.py"}, json={}, timeout=20).status_code
    except httpx.HTTPError as e:
        return repr(e)

with server(repo, PORT) as (base, d, srv):
    cl = admin(base)
    app_id = make_app(cl)
    other = admin(base)          # a second session of the same user
    cookies = cl.c.cookies
    csrf = cookies.get("csrf_token") or ""
    all_routes = routes()
    check("routes listed", len(all_routes) > 80, str(len(all_routes)))
    api = [(m, p) for m, p in all_routes if p.startswith("/api/") and (m, p) not in PUBLIC]

    # ── the key ──
    r = httpx.get(base + "/api/apps", cookies=cookies)
    check("GET /api/apps without key -> 401", r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = httpx.get(base + "/api/apps", cookies=cookies, headers={"x-venvy-key": cl.key})
    check("GET /api/apps right key -> 200", r.status_code == 200, f"{r.status_code}")
    leaked = []
    for method, path in api:
        for label, key in (("no key", None), ("bad key", "garbage"), ("other session's key", other.key)):
            code = call(method, base + fill(path, app_id), cookies, key, csrf)
            if code != 401:
                leaked.append(f"{method} {path} [{label}] -> {code}")
    check("every API route wants this session's key", not leaked, "\n    " + "\n    ".join(leaked))

    # ── roles ──
    for role in ("viewer", "operator"):
        r = cl.req("POST", "/api/users", json={"username": role, "password": "abcdefg12", "role": role})
        assert r.status_code == 200, r.text
    for role, allowed in (("viewer", ANY_ROLE), ("operator", ANY_ROLE | OPERATOR)):
        u = Client(base); u.c.get("/")
        u.key = u.req("POST", "/api/auth/login", json={"username": role, "password": "abcdefg12"}).json()["auth_key"]
        wrong = []
        for method, path in api:
            if (method, path) in allowed:
                continue
            if role == "operator" and method != "GET" and path.startswith("/api/apps"):
                continue         # changing apps is what operators are for
            code = call(method, base + fill(path, app_id), u.c.cookies, u.key, u.c.cookies.get("csrf_token") or "")
            if code != 403:
                wrong.append(f"{method} {path} -> {code}")
        check(f"{role}: everything else is 403", not wrong, "\n    " + "\n    ".join(wrong))
    r = cl.req("GET", "/api/apps")
    check("apps still there after the sweep", any(a["id"] == app_id for a in r.json()), r.text[:200])

    # ── no API map ──
    for p in ("/openapi.json", "/docs", "/redoc"):
        r = httpx.get(base + p)
        check(f"{p} not public", r.status_code in (401, 404), str(r.status_code))

    # ── HTTPS ──
    # The test client connects from 127.0.0.1, which is a trusted proxy by default (cloudflared).
    plain = httpx.Client(base_url=base)
    r = plain.get("/")
    check("plain HTTP: no HSTS", "strict-transport-security" not in r.headers)
    check("plain HTTP: cookie not Secure", "secure" not in r.headers.get("set-cookie", "").lower(), r.headers.get("set-cookie"))
    tls = httpx.Client(base_url=base, headers={"x-forwarded-proto": "https"})
    r = tls.get("/")
    check("via tunnel: HSTS", r.headers.get("strict-transport-security", "").startswith("max-age="), str(dict(r.headers)))
    check("via tunnel: csrf cookie Secure", "secure" in r.headers.get("set-cookie", "").lower(), r.headers.get("set-cookie"))
    # httpx keeps a Secure cookie to itself over plain HTTP, so hand it over.
    tok = tls.cookies.get("csrf_token") or ""
    r = httpx.post(base + "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"},
                   headers={"x-forwarded-proto": "https", "x-csrf-token": tok, "cookie": f"csrf_token={tok}"})
    sc = [v for k, v in r.headers.multi_items() if k == "set-cookie" and v.startswith("session=")]
    check("via tunnel: session cookie Secure", r.status_code == 200 and sc and "secure" in sc[0].lower(), f"{r.status_code} {sc}")
    r = httpx.get(base + "/", headers={"cf-visitor": '{"scheme":"https"}'})
    check("CF-Visitor alone is understood too", "strict-transport-security" in r.headers)

    # ── CSP ──
    r = httpx.get(base + "/")
    csp = r.headers.get("content-security-policy", "")
    script_src = next((d.strip() for d in csp.split(";") if d.strip().startswith("script-src ")), "")
    check("CSP script-src has no 'unsafe-inline'", script_src and "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src, script_src)
    check("CSP has object-src/base-uri/form-action", all(x in csp for x in ("object-src 'none'", "base-uri 'none'", "form-action 'self'")), csp)

print("FAILURES:", fails) if fails else print("ALL OK")
sys.exit(1 if fails else 0)
