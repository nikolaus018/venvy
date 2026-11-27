"""What only a real Linux host (Venvy as root, systemd, nftables, Docker)
can show: apps as services that outlive Venvy, their sandbox, exit codes,
egress rules, app-to-app isolation, the firewall table, shared volumes,
Docker / Compose apps, a managed Redis bound to an app, and toolchains
(Node from nodejs.org, Rust through rustup) with release builds.

Called by t_installed.py (per-app-users mode) with its client and check()."""
import json
import os
import shutil
import subprocess
import time

import httpx

SANDBOX_PROBE = r'''
import os, socket, time
def t(name, fn):
    try:
        fn(); print(name + "=open", flush=True)
    except Exception as e:
        print(name + "=closed", flush=True)
def other_apps():
    # Only the way down to its own folder may be there, nothing beside it.
    here = os.getcwd()
    seen = [n for n in os.listdir(os.path.dirname(here)) if n != os.path.basename(here)]
    seen += [n for n in os.listdir("/var/lib/venvy/apps") if not here.startswith("/var/lib/venvy/apps/" + n + "/")]
    if not seen:
        raise PermissionError("nothing else visible")
t("APPS_DIR", other_apps)
t("SHADOW", lambda: open("/etc/shadow").read(1))
t("USR_WRITE", lambda: open("/usr/venvy-probe", "w"))
t("VENVY_KEY", lambda: open("/var/lib/venvy/.venvy_key").read(1))
print("PROCS=" + str(len([p for p in os.listdir("/proc") if p.isdigit()])), flush=True)
def egress():
    s = socket.create_connection(("1.1.1.1", 53), timeout=4); s.close()
t("EGRESS", egress)
def local():
    s = socket.create_connection(("127.0.0.1", 54783), timeout=4); s.close()
t("LOCAL", local)
def peer():
    s = socket.create_connection(("127.0.0.1", int(os.environ.get("PEER_PORT", "1"))), timeout=4); s.close()
t("PEER", peer)
print("PROBE-DONE", flush=True)
while True: time.sleep(1)
'''

HTTP_APP = r'''
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"PEER-OK")
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", int(os.environ["PORT"])), H).serve_forever()
'''

TICKER = "import time\ni = 0\nwhile True:\n    print('TICK', i, flush=True); i += 1; time.sleep(1)\n"

RUST_MAIN = r'''
use std::io::{Read, Write};
use std::net::TcpListener;
fn main() {
    let port = std::env::var("PORT").unwrap_or("8080".into());
    let l = TcpListener::bind(format!("127.0.0.1:{}", port)).unwrap();
    println!("RUST-UP {}", port);
    for s in l.incoming() {
        let mut s = s.unwrap();
        let mut b = [0u8; 1024];
        let _ = s.read(&mut b);
        let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nRUST-OK");
    }
}
'''


def run(cl, check):
    def make(name, runtime="command", files=None, **settings):
        data = {"name": name, "runtime": runtime, "entrypoint": settings.pop("entrypoint", ""),
                "start_command": settings.pop("start_command", ""), "runtime_version": settings.pop("runtime_version", "")}
        r = cl.req("POST", "/api/apps/upload", data=data)
        assert r.status_code == 200, r.text
        aid = r.json()["app_id"]
        cl.job(r, timeout=900)
        for path, content in (files or {}).items():
            rr = cl.req("POST", f"/api/apps/{aid}/files/op", json={"operation": "save", "path": path, "content": content})
            assert rr.status_code == 200, rr.text
        if settings:
            rr = cl.req("PUT", f"/api/apps/{aid}", json=settings)
            assert rr.status_code == 200, rr.text
        return aid

    def start(aid, timeout=60):
        cl.req("POST", f"/api/apps/{aid}/control?action=start")
        cl.wait_status(aid, "running", timeout)
        return cl.status(aid)

    def logs(aid):
        return cl.req("GET", f"/api/apps/{aid}/logs").json()["content"]

    def wait_log(aid, text, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if text in logs(aid):
                return True
            time.sleep(0.5)
        return False

    def get(port, path="/", timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            try:
                return httpx.get(f"http://127.0.0.1:{port}{path}", timeout=5)
            except Exception:
                time.sleep(1)
        return None

    def deploy(aid, timeout=1800):
        cl.req("POST", f"/api/apps/{aid}/deploy", json={"start": True})
        time.sleep(1)
        end = time.time() + timeout
        while time.time() < end:
            st = cl.req("GET", f"/api/apps/{aid}/deploy").json()["state"]
            if not st["running"] and st["status"] not in (None, "running"):
                return st
            time.sleep(2)
        return st

    # ── an app service outlives Venvy ────────────────────────────────────
    surv = make("Survivor", start_command="python3 ticker.py", files={"ticker.py": TICKER}, restart_policy="always", start_on_boot=True)
    a = start(surv)
    pid = a["pid"]
    check("service app is running", a["status"] == "running" and a.get("runs_as_service"), str(a))
    wait_log(surv, "TICK 2")
    subprocess.run(["systemctl", "restart", "venvy.service"], check=True)
    for _ in range(90):
        try:
            if httpx.get(cl.c.base_url.join("/api/health"), timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    time.sleep(3)
    a = cl.status(surv)
    check("the app kept running while Venvy restarted (same PID)", a["status"] == "running" and a["pid"] == pid, f"{a['status']} {a['pid']} vs {pid}")
    lines = [ln for ln in logs(surv).splitlines() if "TICK" in ln]
    nums = [int(ln.split("TICK")[1]) for ln in lines]
    check("nothing it printed while Venvy was down is lost", nums and nums == list(range(nums[0], nums[-1] + 1)) and nums[-1] >= 5, str(nums[-12:]))
    with open("/var/lib/venvy/.survivor-pid", "w") as f:
        json.dump(pid, f)

    # ── exit codes ───────────────────────────────────────────────────────
    ex = make("Exits", start_command="python3 -c 'import sys; print(\"BYE\", flush=True); sys.exit(3)'")
    cl.req("POST", f"/api/apps/{ex}/control?action=start")
    end = time.time() + 30
    st = cl.status(ex)
    while time.time() < end and st["last_exit_code"] in (None,):
        time.sleep(0.5)
        st = cl.status(ex)
    check("exit code of a service app is recorded", st["status"] == "stopped" and st["last_exit_code"] == 3 and st["crashed"], str(st))
    check("its output reached the log", wait_log(ex, "BYE"))

    # ── stop signal ──────────────────────────────────────────────────────
    sig = make("Signals", start_command="python3 sig.py", stop_signal="SIGINT", files={"sig.py":
              "import signal, sys, time\nsignal.signal(signal.SIGINT, lambda *a: (print('GOT-INT', flush=True), sys.exit(0)))\n"
              "print('UP', flush=True)\nwhile True: time.sleep(0.2)\n"})
    start(sig)
    wait_log(sig, "UP")
    cl.req("POST", f"/api/apps/{sig}/control?action=stop")
    cl.wait_status(sig, "stopped", 30)
    check("the app's stop signal is used", wait_log(sig, "GOT-INT", 10), logs(sig)[-200:])

    # ── sandbox, egress, app-to-app isolation, firewall ─────────────────
    peer = make("Peer", start_command="python3 peer.py", files={"peer.py": HTTP_APP}, bind_public=False)
    pa = start(peer)
    peer_port = pa["port"]
    check("root on the host reaches the private app", get(peer_port) is not None and get(peer_port).text == "PEER-OK")
    box = make("Sandboxed", start_command="python3 probe.py", files={"probe.py": SANDBOX_PROBE}, egress_policy="deny",
               env_vars={"PEER_PORT": str(peer_port)})
    start(box)
    wait_log(box, "PROBE-DONE", 40)
    out = logs(box)
    check("sandbox: other apps' folders are not visible", "APPS_DIR=closed" in out, out[-600:])
    check("sandbox: /etc/shadow unreadable, /usr read-only", "SHADOW=closed" in out and "USR_WRITE=closed" in out)
    check("sandbox: Venvy's key out of reach", "VENVY_KEY=closed" in out)
    procs = int(next((ln.split("PROCS=")[1] for ln in out.splitlines() if "PROCS=" in ln), "999"))
    check("sandbox: other processes are hidden", procs < 20, str(procs))
    check("egress deny: no connections out", "EGRESS=closed" in out)
    check("egress deny: this host still reachable", "LOCAL=open" in out)
    check("another app's port is closed to this app", "PEER=closed" in out)
    nft = subprocess.run(["nft", "list", "table", "inet", "venvy"], capture_output=True, text=True).stdout
    check("firewall: the private app's port is closed to the network", str(peer_port) in nft and "drop" in nft, nft[-800:])
    cl.req("POST", f"/api/apps/{box}/control?action=stop")

    # ── shared volume between two apps ───────────────────────────────────
    v = cl.req("POST", "/api/volumes", json={"name": "shared-ci"}).json()
    w1 = make("Vol writer", start_command="sleep 600")
    w2 = make("Vol reader", start_command="sleep 600")
    for w in (w1, w2):
        r = cl.req("POST", f"/api/apps/{w}/volumes/{v['id']}", json={"read_only": w == w2})
        check(f"volume attached to app {w}", r.status_code == 200, r.text)
    r1 = cl.job(cl.req("POST", f"/api/apps/{w1}/run", json={"command": 'echo hello-volume > "$VOLUME_SHARED_CI/x.txt" && echo wrote'}))
    r2 = cl.job(cl.req("POST", f"/api/apps/{w2}/run", json={"command": 'cat "$VOLUME_SHARED_CI/x.txt"'}))
    check("one app writes, the other reads the shared volume", "wrote" in r1["output"] and "hello-volume" in r2["output"], f"{r1} {r2}")
    outsider = make("Not attached", start_command="sleep 600")
    r3 = cl.job(cl.req("POST", f"/api/apps/{outsider}/run", json={"command": "cat /var/lib/venvy/volumes/shared-ci/x.txt; echo rc=$?"}))
    check("an app not attached can't read it", "hello-volume" not in r3["output"], r3["output"])

    # ── Docker, Compose, a managed service ───────────────────────────────
    if shutil.which("docker") and subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
        d1 = make("Nginx image", runtime="docker", entrypoint="nginx:alpine", container_port=80)
        da = start(d1, 180)
        r = get(da["port"], timeout=180)
        check("docker image app is served on its port", r is not None and "nginx" in r.text.lower(), logs(d1)[-400:])
        cl.req("POST", f"/api/apps/{d1}/control?action=stop")
        cl.wait_status(d1, "stopped", 60)
        gone = subprocess.run(["docker", "ps", "-q", "--filter", f"name=venvy-app-{d1}"], capture_output=True, text=True).stdout.strip()
        check("its container is gone after stop", gone == "", gone)
        d2 = make("Dockerfile app", runtime="docker", container_port=8080, files={
            "Dockerfile": "FROM busybox:stable\nCOPY index.html /www/index.html\nCMD [\"httpd\", \"-f\", \"-p\", \"8080\", \"-h\", \"/www\"]\n",
            "index.html": "BUILT-FROM-DOCKERFILE"})
        db = start(d2, 300)
        r = get(db["port"], timeout=120)
        check("Dockerfile is built and run", r is not None and "BUILT-FROM-DOCKERFILE" in r.text, logs(d2)[-600:])
        d3 = make("Compose app", runtime="compose", files={
            "compose.yaml": "services:\n  web:\n    image: busybox:stable\n    command: [\"sh\", \"-c\", \"mkdir -p /w && echo COMPOSE-OK > /w/index.html && httpd -f -p 8080 -h /w\"]\n"
                            "    ports: [\"127.0.0.1:${PORT}:8080\"]\n"})
        dc = start(d3, 300)
        r = get(dc["port"], timeout=120)
        check("compose project runs, with $PORT in the file", r is not None and "COMPOSE-OK" in r.text, logs(d3)[-600:])
        for x in (d2, d3):
            cl.req("POST", f"/api/apps/{x}/control?action=stop")
        svc = cl.req("POST", "/api/services", json={"template": "redis", "name": "ci-redis"})
        check("managed Redis created", svc.status_code == 200, svc.text)
        sid = svc.json()["id"]
        cl.wait_status(sid, "running", 180)
        user = make("Uses redis", start_command="sleep 600")
        r = cl.req("POST", f"/api/apps/{user}/services", json={"service_app_id": sid})
        check("service bound to an app", r.status_code == 200, r.text)
        probe = ("python3 -c \"import os,socket,urllib.parse as u; p=u.urlparse(os.environ['REDIS_URL']); "
                 "s=socket.create_connection((p.hostname,p.port),5); "
                 "s.sendall(('AUTH '+u.unquote(p.password)+'\\r\\nPING\\r\\n').encode()); import time; time.sleep(0.5); print(s.recv(100))\"")
        res = None
        for _ in range(30):
            res = cl.job(cl.req("POST", f"/api/apps/{user}/run", json={"command": probe}))
            if "PONG" in res["output"]:
                break
            time.sleep(2)
        check("the app reaches Redis with REDIS_URL", res and "PONG" in res["output"], str(res))
        import psutil
        pw = json.loads(cl.status(sid)["env_json"]).get("REDIS_PASSWORD", "")
        leaked = []
        for proc in psutil.process_iter(["cmdline"]):
            if pw and pw in " ".join(proc.info.get("cmdline") or []):
                leaked.append(proc.pid)
        check("the Redis password is in no command line on the host", pw and not leaked, str(leaked))
        cl.req("DELETE", f"/api/apps/{user}/services/" + str(cl.req("GET", f"/api/apps/{user}/services").json()[0]["id"]))
        cl.req("POST", f"/api/apps/{sid}/control?action=stop")
    else:
        print("  skip docker checks (no Docker here)")

    # ── toolchains: Node from nodejs.org, Rust through rustup ────────────
    r = cl.job(cl.req("POST", "/api/toolchains", json={"kind": "node", "version": "20"}), timeout=900)
    check("node 20 installed as a toolchain", str(r.get("version", "")).startswith("20."), str(r))
    na = make("Node 20", runtime="node", entrypoint="server.js", runtime_version="20", files={
        "package.json": '{"name": "t", "version": "1.0.0"}',
        "server.js": "require('http').createServer((q, s) => s.end(process.version)).listen(process.env.PORT);"})
    a = start(na, 120)
    rr = get(a["port"])
    check("the app runs on the Node version it asked for", rr is not None and rr.text.startswith("v20."), getattr(rr, "text", logs(na)[-300:]))
    cl.req("POST", f"/api/apps/{na}/control?action=stop")
    r = cl.job(cl.req("POST", "/api/toolchains", json={"kind": "rust", "version": "stable"}), timeout=1800)
    check("rust stable installed with rustup", r.get("version") == "stable", str(r))
    ra = make("Rust release", runtime="rust", runtime_version="stable", files={
        "Cargo.toml": '[package]\nname = "ciapp"\nversion = "0.1.0"\nedition = "2021"\n',
        "src/main.rs": RUST_MAIN}, healthcheck_path="/")
    st = deploy(ra)
    check("rust app built with cargo build --release and deployed", st["status"] == "ok", "\n".join(st["log"][-20:]))
    a = cl.status(ra)
    rr = get(a["port"]) if a["port"] else None
    check("... and runs the release binary", rr is not None and rr.text == "RUST-OK"
          and os.path.exists(os.path.join(a["app_dir"], "target/release/ciapp")), getattr(rr, "text", ""))
    check("... its cargo caches are outside the app folder", not os.path.exists(os.path.join(a["app_dir"], ".cargo")))

    # Tidy up: only the first app and the Survivor stay for the update checks.
    for aid in (ex, sig, peer, box, w1, w2, outsider, na, ra):
        cl.req("POST", f"/api/apps/{aid}/control?action=stop")
        cl.req("DELETE", f"/api/apps/{aid}")
    for x in cl.req("GET", "/api/apps").json():
        if x["name"] not in ("Installed App", "Survivor") and not x.get("parent_app_id"):
            cl.req("POST", f"/api/apps/{x['id']}/control?action=stop")
            cl.req("DELETE", f"/api/apps/{x['id']}")
    for b in cl.req("GET", "/api/volumes").json():
        cl.req("DELETE", f"/api/volumes/{b['id']}?purge=true")
