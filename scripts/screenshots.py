"""Retakes the README's screenshots (docs/screenshots/) from a real Venvy.

Starts Venvy on a throwaway data directory, creates a few demo apps (Python,
Node.js, a static site), lets them run long enough for the charts to have a
history, and photographs every view with headless Chrome: desktop at
1440x900, phones at 390x844, both at device resolution.

    pip install -r requirements.txt -r tests/requirements.txt   # playwright, an installed Chrome
    python scripts/screenshots.py            # takes about four minutes

Node.js should be installed for the Node demo app; without it that app is
left out.

Taken on a workstation, which has neither per-app Unix users (Linux, run as
root) nor systemd, the app list is shown as a default install reports it:
`isolated` and `resource_limits_enforced` true. Nothing else is altered.
"""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
from harness import admin, free_port, server  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = REPO / "docs" / "screenshots"
PORT = 55090
WARMUP_SEC = 150          # history for the charts (a sample every 5 s)

SHOP = r'''import json, os, random, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
G, Y, C, D, R = "\033[32m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"
PRODUCTS = [
    {"id": 1, "name": "Espresso beans", "price": 14.5},
    {"id": 2, "name": "Pour-over kettle", "price": 39.0},
    {"id": 3, "name": "Ceramic mug", "price": 12.0},
]


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, {"status": "ok"})
        if self.path == "/api/products":
            return self.send(200, PRODUCTS)
        self.send(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        print(f"{G}INFO{R}  {C}{self.command:<4}{R} {self.path}  {D}{args[1]}{R}", flush=True)


def orders():
    n = 1041
    while True:
        time.sleep(random.uniform(1.5, 4))
        n += 1
        if random.random() < 0.12:
            print(f"{Y}WARN{R}  payment provider slow ({random.randint(900, 2400)} ms) for order #{n}", flush=True)
        else:
            print(f"{G}INFO{R}  order #{n} paid: {random.randint(1, 4)} item(s), EUR {random.uniform(12, 140):.2f}", flush=True)


print(f"{G}INFO{R}  Shop API listening on :{PORT}", flush=True)
threading.Thread(target=orders, daemon=True).start()
ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
'''

CHAT = r'''const http = require("http");
const port = process.env.PORT || 3000;
const people = ["anna", "ben", "chris", "dana", "eli"];
let n = 0;
http.createServer((req, res) => {
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify({ ok: true, online: 3 + (n % 5) }));
}).listen(port, () => console.log(`chat server listening on :${port}`));
setInterval(() => {
  n++;
  console.log(`[ws] ${people[n % people.length]} ${n % 3 ? "joined" : "left"} #general (${3 + (n % 5)} online)`);
}, 4000);
'''

REPORTS = r'''import datetime
print(f"Sales report for {datetime.date.today() - datetime.timedelta(days=1)}: 132 orders, EUR 6,412.80")
'''

SITE = '''<!doctype html><meta charset="utf-8"><title>Storefront</title>
<h1>Coffee & Co.</h1><p>Fresh beans, roasted every Tuesday.</p>
'''


def make(cl, name, runtime, entry, files, **settings):
    r = cl.req("POST", "/api/apps/upload", data={"name": name, "runtime": runtime, "entrypoint": entry})
    app_id = r.json()["app_id"]
    cl.job(r)
    for path, content in files.items():
        r = cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": path, "content": content})
        assert r.status_code == 200, r.text
    if settings:
        r = cl.req("PUT", f"/api/apps/{app_id}", json=settings)
        assert r.status_code == 200, r.text
    return app_id


def main() -> None:
    env = {"VENVY_METRICS_FLUSH_SEC": "5", "VENVY_HEALTH_WAIT_SEC": "10", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    with server(str(REPO), PORT, env=env) as (base, data, srv):
        cl = admin(base)
        shop_port, chat_port = free_port(), free_port()
        shop = make(cl, "Shop API", "python", "main.py", {
            "main.py": SHOP,
            "requirements.txt": "# standard library only\n",
            "README.md": "# Shop API\n\nProducts and orders for the Coffee & Co. storefront.\n",
            "config/settings.json": '{\n  "currency": "EUR",\n  "page_size": 20\n}\n',
            "static/logo.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>\n",
        }, manual_ports=[shop_port], healthcheck_path="/health", health_interval=10,
            restart_policy="always", start_on_boot=True, memory_limit_mb=512, cpu_limit_percent=100,
            env_vars={"DATABASE_URL": "postgresql://shop@localhost/shop", "STRIPE_KEY": "sk_test_demo"})
        apps = [shop]
        if shutil.which("node"):
            apps.append(make(cl, "Realtime Chat", "node", "server.js", {
                "server.js": CHAT, "package.json": '{\n  "name": "realtime-chat",\n  "private": true\n}\n',
            }, manual_ports=[chat_port]))
        apps.append(make(cl, "Storefront", "static", "", {"public/index.html": SITE}))
        reports = make(cl, "Nightly Reports", "python", "main.py", {"main.py": REPORTS})
        cl.req("POST", f"/api/apps/{reports}/jobs", json={"name": "Daily sales report", "schedule": "0 6 * * *",
                                                           "command": "python main.py"})
        for a in apps:
            cl.req("POST", f"/api/apps/{a}/control?action=start")
        for a in apps:
            cl.wait_status(a, "running", 60)

        # A deploy and a release to show on the Deploy tab.
        cl.req("PUT", f"/api/apps/{shop}/git", json={"build_command": "python -c \"import ast; ast.parse(open('main.py').read())\" && echo syntax ok"})
        cl.req("POST", f"/api/apps/{shop}/deploy", json={})

        # Some traffic, for the logs and the charts.
        stop = threading.Event()

        def traffic():
            import httpx
            while not stop.is_set():
                for path in ("/api/products", "/api/products", "/api/cart"):
                    try:
                        httpx.get(f"http://127.0.0.1:{shop_port}{path}", timeout=2)
                    except Exception:
                        pass
                stop.wait(2.5)
        threading.Thread(target=traffic, daemon=True).start()
        print(f"Letting the apps run for {WARMUP_SEC} s so the charts have a history…")
        time.sleep(WARMUP_SEC)

        OUT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            br = p.chromium.launch(channel="chrome", headless=True)

            def as_installed(route):
                resp = route.fetch()
                try:
                    data = resp.json()
                except Exception:
                    return route.fulfill(response=resp)
                for a in data if isinstance(data, list) else [data]:
                    if isinstance(a, dict) and "isolated" in a:
                        a.update(isolated=True, resource_limits_enforced=True)
                route.fulfill(response=resp, body=json.dumps(data))

            def signed_in(**ctx):
                c = br.new_context(**ctx)
                c.route("**/api/apps", as_installed)
                c.route("**/api/apps/*[0-9]", as_installed)
                pg = c.new_page()
                pg.goto(base + "/")
                pg.wait_for_selector("#username")
                pg.fill("#username", "admin")
                pg.fill("#password", "hunter2hunter2")
                pg.click("#login-submit-btn")
                pg.wait_for_selector("#app-shell", state="visible")
                pg.wait_for_timeout(1500)
                return c, pg

            def shot(pg, name, wait=900):
                pg.mouse.move(5, pg.viewport_size["height"] - 5)    # no hover highlight in the picture
                pg.wait_for_timeout(wait)
                pg.screenshot(path=str(OUT / f"{name}.png"))
                print("  ", name)

            def open_app(pg, app_id):
                pg.evaluate(f"selectApp({app_id})")
                pg.wait_for_timeout(1500)

            def type_commands(pg, commands):
                for c in commands:
                    pg.keyboard.type(c + "\n", delay=8)
                    pg.wait_for_timeout(1300)

            terminal = ["clear", "ls", "python --version",
                        f"curl -s localhost:{shop_port}/api/products | python -m json.tool",
                        "cat config/settings.json"]

            ctx, pg = signed_in(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="dark")
            pg.evaluate("setTheme('dark')")
            open_app(pg, shop)
            pg.wait_for_function("window.term && termSocket && termSocket.readyState === 1", timeout=15000)
            type_commands(pg, terminal)
            shot(pg, "terminal")
            pg.click("#tabbtn-files"); pg.wait_for_timeout(900)
            pg.click("#file-tree .file-row[data-name='main.py']")
            shot(pg, "files", 1500)
            pg.click("#tabbtn-logs")
            shot(pg, "logs", 2000)
            pg.evaluate("switchTab('deploy')")
            shot(pg, "deploy", 1500)
            pg.evaluate("switchTab('settings')")
            pg.wait_for_timeout(2000)
            pg.evaluate("document.getElementById('usage-cpu').closest('.card').scrollIntoView({block: 'start'})")
            shot(pg, "settings", 1200)
            open_app(pg, reports)
            pg.evaluate("switchTab('jobs')")
            shot(pg, "jobs", 1200)
            pg.click("#system-nav-btn")
            pg.wait_for_timeout(3000)
            pg.evaluate("document.getElementById('sys-history-card').scrollIntoView({block: 'start'})")
            shot(pg, "system", 1200)
            open_app(pg, shop)
            pg.evaluate("switchTab('logs')")
            pg.wait_for_timeout(800)
            pg.evaluate("openPalette()")
            shot(pg, "palette", 700)
            pg.keyboard.press("Escape")
            pg.evaluate("setTheme('light')")
            pg.evaluate("switchTab('logs')")
            shot(pg, "light", 2000)
            pg.evaluate("setTheme('dark')")
            ctx.unroute_all(behavior="ignoreErrors")
            ctx.close()

            phone = {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True,
                     "has_touch": True, "color_scheme": "dark"}
            ctx, pg = signed_in(**phone)
            pg.evaluate("setTheme('dark')")
            pg.evaluate("toggleSidebar()")
            shot(pg, "mobile-sidebar", 900)
            pg.evaluate("toggleSidebar()")
            open_app(pg, shop)
            pg.wait_for_function("window.term && termSocket && termSocket.readyState === 1", timeout=15000)
            type_commands(pg, ["clear", "ls", f"curl -s localhost:{shop_port}/health; echo"])
            shot(pg, "mobile-terminal")
            pg.evaluate("switchTab('logs')")
            shot(pg, "mobile-logs", 2000)
            ctx.unroute_all(behavior="ignoreErrors")
            ctx.close()
            br.close()
        stop.set()


if __name__ == "__main__":
    main()
