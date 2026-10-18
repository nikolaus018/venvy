"""The runtime table: what it says to run, the static file server, and an
app of every runtime whose tools this machine has (the rest are checked to be
refused with an install hint)."""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time
from pathlib import Path
import httpx
from harness import Client, admin, server
repo = sys.argv[1]; fails = []
sys.path.insert(0, repo)
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

# ── 1. the table itself (no server) ─────────────────────────────────────────
from venvy import runtimes as rt
EXPECT = ["python", "node", "bun", "deno", "rust", "go", "php", "ruby", "java", "static"]
check("all runtimes present", list(rt.RUNTIMES) == EXPECT, str(list(rt.RUNTIMES)))
A = Path("/srv/app")
cmd = lambda k, e="", port=8123: rt.start_command(k, A, e, port, "/srv/app/venv")
check("python cmd", cmd("python", "main.py") == ["/srv/app/venv/bin/python", "main.py"])
check("node cmd / npm start", cmd("node", "s.js") == ["node", "s.js"] and cmd("node")[:2] == ["npm", "start"])
check("bun cmd", cmd("bun", "i.ts") == ["bun", "run", "./i.ts"] and cmd("bun") == ["bun", "run", "start"])
check("deno cmd", cmd("deno", "m.ts") == ["deno", "run", "--allow-all", "m.ts"] and cmd("deno") == ["deno", "task", "start"])
check("go/rust/java cmd", cmd("go") == ["go", "run", "."] and cmd("rust") == ["cargo", "run"] and cmd("java", "a.jar") == ["java", "-jar", "a.jar"])
check("ruby cmd", cmd("ruby", "app.rb") == ["ruby", "app.rb"]
      and cmd("ruby", "config.ru")[-5:] == ["--host", "0.0.0.0", "--port", "8123", "config.ru"][-5:]
      and "rackup" in cmd("ruby", "config.ru"))
check("php router default / own", cmd("php")[:3] == ["php", "-S", "0.0.0.0:8123"] and cmd("php")[-1].endswith("router.php")
      and cmd("php", "front.php")[-1] == "front.php")
st = cmd("static")
check("static cmd names port and folder", st[0] == "python3" and "--port" in st and st[st.index("--port") + 1] == "8123", str(st))
check("helper scripts ship", Path(repo, "venvy/runtime_assets/static_server.py").is_file() and Path(repo, "venvy/runtime_assets/router.php").is_file())
check("needs_port", rt.needs_port("static", "") and rt.needs_port("php", "") and rt.needs_port("ruby", "config.ru")
      and not rt.needs_port("ruby", "app.rb") and not rt.needs_port("node", ""))
for key, bad in [("python", "../x.py"), ("node", "a/b.js"), ("static", "x y"), ("go", None)]:
    if bad is None: continue
    try: rt.clean_entrypoint(key, bad); ok = False
    except ValueError: ok = True
    check(f"{key} entrypoint {bad!r} refused", ok)
check("fixed entrypoints", rt.clean_entrypoint("rust", "whatever") == "main.rs" and rt.clean_entrypoint("go", "x") == "main.go")
check("blank entrypoint ok", rt.clean_entrypoint("node", " ") == "" and rt.clean_entrypoint("python", "") == "")
def refused(fn, *a):
    try: fn(*a); return False
    except ValueError: return True
for key, good, bad in [
    ("node", ["express", "express@4", "@scope/pkg@^1.2.0", "JSONStream"], ["--foo", "-g", "a b", "../x", "x;rm", "@/x"]),
    ("bun", ["hono@4"], ["-x", "a b"]),
    ("deno", ["jsr:@std/path", "npm:express@4", "jsr:@std/path@^1"], ["express", "-A", "https://x.com/m.ts", "npm:"]),
    ("go", ["github.com/gin-gonic/gin", "github.com/gin-gonic/gin@v1.10.0", "golang.org/x/text@latest"], ["-u", "a b", "x;y", "../x", "http://x"]),
    ("php", ["monolog/monolog", "monolog/monolog:^3.0", "a/b@dev"], ["monolog", "-x/y", "A/B", "a/b c"]),
    ("ruby", ["sinatra", "sinatra@4.0", "rack-test@~>2.0"], ["-v", "a b", "a;b", "a/b"]),
]:
    for g in good: check(f"{key} accepts {g}", not refused(rt.install_steps, key, A, g))
    for b in bad: check(f"{key} refuses {b!r}", refused(rt.install_steps, key, A, b))
check("composer @ becomes :", rt.install_steps("php", A, "a/b@^1")[0].argv[2] == "a/b:^1")
check("ruby version flag", rt.install_steps("ruby", A, "sinatra@4.0")[0].argv == ["bundle", "add", "sinatra", "--version", "4.0"])
check("go remove drops then tidies", [s.argv[:3] for s in rt.remove_steps("go", A, "github.com/a/b")] == [["go", "mod", "edit"], ["go", "mod", "tidy"]])
check("remove names checked", refused(rt.remove_steps, "node", A, "--x") and refused(rt.remove_steps, "php", A, "nope"))
files = {
    "package.json": '{"dependencies": {"a": "^1"}, "devDependencies": {"b": "2"}}',
    "deno.jsonc": '{\n // comment "x"\n "imports": {"@std/path": "jsr:@std/path@^1", /* c */ "oak": "https://deno.land/x/oak/mod.ts",},\n}',
    "go.mod": 'module x\n\ngo 1.22\n\nrequire (\n\tgithub.com/a/b v1.2.3\n\tgithub.com/c/d v0.1.0 // indirect\n)\nrequire github.com/e/f v2.0.0 // direct\n',
    "composer.json": '{"require": {"php": ">=8.1", "ext-json": "*", "monolog/monolog": "^3"}, "require-dev": {"phpunit/phpunit": "^11"}}',
    "Gemfile": "source 'https://rubygems.org'\ngem 'sinatra', '~> 4.0'\n  gem \"puma\"\n# gem 'nope'\n",
    "Gemfile.lock": "GEM\n  remote: https://rubygems.org/\n  specs:\n    sinatra (4.0.1)\n      rack (>= 3)\n    puma (6.4.2)\n",
}
rd = lambda n: files.get(n)
names = lambda k: [(p["name"], p["version"]) for p in rt.list_packages(k, rd)]
check("package.json listed", names("node") == [("a", "^1"), ("b", "2")] and names("bun") == names("node"))
check("deno.jsonc listed (comments, trailing comma)", names("deno")[:2] == [("@std/path", "jsr:@std/path@^1"), ("oak", "https://deno.land/x/oak/mod.ts")], str(names("deno")))
check("go.mod listed (no indirect)", names("go") == [("github.com/a/b", "v1.2.3"), ("github.com/e/f", "v2.0.0")], str(names("go")))
check("composer listed (no php/ext)", names("php") == [("monolog/monolog", "^3"), ("phpunit/phpunit", "^11")], str(names("php")))
check("Gemfile listed, locked version wins", names("ruby") == [("sinatra", "4.0.1"), ("puma", "6.4.2")], str(names("ruby")))
check("no manifest, no packages", all(rt.list_packages(k, lambda n: None) == [] for k in EXPECT))
d = Path(tempfile.mkdtemp(prefix="rt-"))
(d / "package.json").write_text("{}")
check("first run installs node deps", len(rt.first_run_steps("node", d)) == 1)
(d / "node_modules").mkdir()
check("... not when they are there", rt.first_run_steps("node", d) == [] and rt.first_run_steps("go", d) == [])
shutil.rmtree(d)

# ── 2. the static file server on its own ────────────────────────────────────
site = Path(tempfile.mkdtemp(prefix="site-"))
(site / "index.html").write_text("HOME"); (site / "about.html").write_text("ABOUT")
(site / ".env").write_text("SECRET"); (site / "logs").mkdir(); (site / "logs/app.log").write_text("LOG")
(site / "empty").mkdir(); (site / "docs").mkdir(); (site / "docs/index.html").write_text("DOCS")
os.symlink("/etc/hosts", site / "escape.txt")
port = free_port()
srv = subprocess.Popen([sys.executable, "-I", str(Path(repo, "venvy/runtime_assets/static_server.py")),
                        "--dir", str(site), "--host", "127.0.0.1", "--port", str(port), "--deny", "logs"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
try:
    for _ in range(50):
        try: httpx.get(f"http://127.0.0.1:{port}/", timeout=1); break
        except Exception: time.sleep(0.1)
    g = lambda p, **kw: httpx.get(f"http://127.0.0.1:{port}{p}", **kw)
    check("static: home", g("/").text == "HOME")
    check("static: clean URL", g("/about").text == "ABOUT")
    check("static: folder index", g("/docs/").text == "DOCS")
    check("static: folder without slash redirects", g("/docs", follow_redirects=False).status_code in (301, 302))
    check("static: no listing", g("/empty/").status_code == 404)
    check("static: dotfile hidden", g("/.env").status_code == 404 and g("/%2eenv").status_code == 404)
    check("static: denied folder", g("/logs/app.log").status_code == 404)
    check("static: symlink out of the folder", g("/escape.txt").status_code == 404)
    check("static: traversal", g("/..%2f..%2fetc/hosts").status_code in (400, 404))
    check("static: unknown is 404", g("/nope").status_code == 404)
    check("static: no-cache header", g("/").headers.get("cache-control") == "no-cache")
    check("static: POST refused", httpx.post(f"http://127.0.0.1:{port}/", content=b"x").status_code in (405, 501))
    (site / "404.html").write_text("CUSTOM404")
    r = g("/nope"); check("static: custom 404 page", r.status_code == 404 and r.text == "CUSTOM404")
    (site / "200.html").write_text("SPA")
    r = g("/some/route"); check("static: 200.html is the SPA fallback", r.status_code == 200 and r.text == "SPA")
finally:
    srv.terminate(); srv.wait(5); shutil.rmtree(site, ignore_errors=True)

# ── 3. through the API ──────────────────────────────────────────────────────
with server(repo, 55011) as (base, d, srv):
    cl = admin(base)
    lst = cl.req("GET", "/api/runtimes").json()
    check("/api/runtimes lists every runtime", [r["key"] for r in lst] == EXPECT)
    check("... and says what is installed", all(r["installed"] == (not rt.missing_tools(r["key"])) for r in lst))
    check("/api/runtimes needs a login", httpx.get(base + "/api/runtimes").status_code in (401, 403))
    check("unknown runtime refused", cl.req("POST", "/api/apps/upload", data={"name": "X", "runtime": "cobol"}).status_code == 400)

    # Every runtime this machine lacks is refused with the hint; the rest are created.
    have = []
    for r in lst:
        if r["installed"]:
            have.append(r["key"]); continue
        resp = cl.req("POST", "/api/apps/upload", data={"name": f"Missing {r['key']}", "runtime": r["key"]})
        check(f"{r['key']}: refused when not installed, with a hint", resp.status_code == 400 and "not installed" in resp.text and "Install" in resp.text, resp.text)

    def make(runtime, name=None, entry="", files=None, **kw):
        r = cl.req("POST", "/api/apps/upload", data={"name": name or f"T {runtime}", "runtime": runtime, "entrypoint": entry})
        assert r.status_code == 200, r.text
        aid = r.json()["app_id"]; cl.job(r)
        for path, content in (files or {}).items():
            assert cl.req("POST", f"/api/apps/{aid}/files/op", json={"operation": "save", "path": path, "content": content}).status_code == 200
        if kw: assert cl.req("PUT", f"/api/apps/{aid}", json=kw).status_code == 200
        return aid
    def run(aid, timeout=40):
        cl.req("POST", f"/api/apps/{aid}/control?action=start")
        end = time.time() + timeout
        while time.time() < end:
            a = cl.status(aid)
            if a["status"] == "running" and a["port"]: return a
            if a["status"] == "error": return a
            time.sleep(0.4)
        return cl.status(aid)
    def fetch(a, path="/", tries=40):
        for _ in range(tries):
            try: return httpx.get(f"http://127.0.0.1:{a['port']}{path}", timeout=2)
            except Exception: time.sleep(0.4)
    def logs(aid): return cl.req("GET", f"/api/apps/{aid}/logs").json()["content"]
    def stop(aid):
        cl.req("POST", f"/api/apps/{aid}/control?action=stop"); cl.wait_status(aid, "stopped")

    # static: needs only python3
    sid = make("static", files={"index.html": "HELLO-STATIC", "assets/x.txt": "x"})
    a = run(sid)
    check("static app runs and got a port", a["status"] == "running" and a["port"], str(a))
    r = fetch(a)
    check("static app serves the app folder when there is no public/dist/...", r is not None and r.text == "HELLO-STATIC", getattr(r, "text", None))
    check("static app does not serve its log", fetch(a, "/logs/app.log").status_code == 404)
    stop(sid)
    check("static: folder setting is validated at start",
          cl.req("PUT", f"/api/apps/{sid}", json={"entrypoint": "nope"}).status_code == 200
          and run(sid)["status"] == "error")
    cl.req("PUT", f"/api/apps/{sid}", json={"entrypoint": "assets"})
    a = run(sid); r = fetch(a, "/x.txt")
    check("static: named folder is served", r is not None and r.text == "x")
    stop(sid)
    check("static: entrypoint with a path refused", cl.req("PUT", f"/api/apps/{sid}", json={"entrypoint": "../x"}).status_code == 400)
    check("static: no packages (list empty, install 400)", cl.req("GET", f"/api/apps/{sid}/packages").json() == []
          and cl.req("POST", f"/api/apps/{sid}/packages", json={"name": "x"}).status_code == 400
          and cl.req("POST", f"/api/apps/{sid}/packages/sync").status_code == 400)

    did_ = make("static", name="Dist", files={"dist/index.html": "FROM-DIST", "index.html": "ROOT"})
    r = fetch(run(did_)); check("static: dist/ is picked over the app folder", r is not None and r.text == "FROM-DIST")
    stop(did_)

    # runtime switching
    rid = make("static", name="Switcher")
    cl.req("PUT", f"/api/apps/{rid}", json={"runtime": "rust"})
    check("fixed entrypoint set on switch", cl.status(rid)["entrypoint"] == "main.rs")
    cl.req("PUT", f"/api/apps/{rid}", json={"runtime": "go"})
    check("fixed -> fixed", cl.status(rid)["entrypoint"] == "main.go")
    cl.req("PUT", f"/api/apps/{rid}", json={"runtime": "node"})
    check("fixed -> free clears the old value", cl.status(rid)["entrypoint"] == "")
    check("runtime+entrypoint together", cl.req("PUT", f"/api/apps/{rid}", json={"runtime": "ruby", "entrypoint": "x.rb"}).json()["entrypoint"] == "x.rb")
    check("switch to unknown refused", cl.req("PUT", f"/api/apps/{rid}", json={"runtime": "cobol"}).status_code == 400)

    # package lists need no tools
    for key, mf, content, want in [
        ("go", "go.mod", files["go.mod"], "github.com/a/b"),
        ("ruby", "Gemfile", files["Gemfile"], "sinatra"),
        ("php", "composer.json", files["composer.json"], "monolog/monolog"),
        ("deno", "deno.json", '{"imports": {"x": "jsr:@std/x"}}', "x"),
    ]:
        aid = make("static", name=f"Pk {key}", files={mf: content})
        cl.req("PUT", f"/api/apps/{aid}", json={"runtime": key})
        got = [p["name"] for p in cl.req("GET", f"/api/apps/{aid}/packages").json()]
        check(f"{key}: packages read from {mf}", want in got, str(got))
        if rt.missing_tools(key, packages=True):
            resp = cl.req("POST", f"/api/apps/{aid}/packages", json={"name": "x/y" if key in ("php", "go") else "xy"})
            if resp.status_code == 200:
                try: cl.job(resp); ok = False
                except RuntimeError as e: ok = "not installed" in str(e) or "failed" in str(e)
            else: ok = resp.status_code == 400
            check(f"{key}: package manager missing -> clear error", ok)
        check(f"{key}: bad package name refused", cl.req("POST", f"/api/apps/{aid}/packages", json={"name": "--evil"}).status_code == 400)

    # first start installs the dependencies
    if "node" in have:
        nid = make("node", entry="server.js", files={
            "package.json": '{"name": "t", "version": "1.0.0"}',
            "server.js": 'require("http").createServer((q,s)=>s.end("NODE-OK")).listen(process.env.PORT);'},
                   manual_ports=[free_port()])
        a = run(nid, 90)
        check("node: first start ran npm install", "npm install finished" in logs(nid), logs(nid)[-400:])
        r = fetch(a)
        check("node: serves on the assigned PORT", r is not None and r.text == "NODE-OK", getattr(r, "text", ""))
        stop(nid)
        check("npm scoped uninstall reaches the route", cl.req("DELETE", f"/api/apps/{nid}/packages/%40scope%2Fpkg").status_code == 200)
        check("npm sync", cl.req("POST", f"/api/apps/{nid}/packages/sync").status_code == 200)

    if "deno" in have:
        did = make("deno", entry="main.ts", files={"main.ts": 'Deno.serve({port: Number(Deno.env.get("PORT"))}, () => new Response("DENO-OK"));'}, manual_ports=[free_port()])
        a = run(did, 60)
        r = fetch(a, tries=60)
        check("deno: runs the entrypoint with PORT", r is not None and r.text == "DENO-OK", logs(did)[-400:])
        stop(did)
        did2 = make("deno", name="Deno task", files={"deno.json": '{"tasks": {"start": "echo TASK-RAN && sleep 30"}}'})
        a = run(did2, 30); time.sleep(1)
        check("deno: blank entrypoint runs the start task", "TASK-RAN" in logs(did2), logs(did2)[-300:])
        stop(did2)

    if "ruby" in have:
        yid = make("ruby", entry="app.rb", files={"app.rb": 'require "socket"\ns = TCPServer.new("0.0.0.0", ENV["PORT"].to_i)\nputs "RUBY-UP #{ENV["PORT"]}"; $stdout.flush\nloop { c = s.accept; c.read_nonblock(1024) rescue nil; c.write("HTTP/1.1 200 OK\\r\\nContent-Length: 7\\r\\nConnection: close\\r\\n\\r\\nRUBY-OK"); c.close }'},
                   manual_ports=[free_port()])
        a = run(yid, 30)
        r = fetch(a)
        check("ruby: runs the entrypoint with PORT", r is not None and r.text == "RUBY-OK", logs(yid)[-300:])
        stop(yid)

    if "java" in have:
        jid = make("java", entry="missing.jar")
        a = run(jid, 20); time.sleep(1)
        check("java: runs java -jar (a missing jar fails visibly)", cl.status(jid)["status"] in ("error", "stopped") or "Unable to access jarfile" in logs(jid), logs(jid)[-200:])

    # a required entrypoint is demanded; an unset one is a clear error
    yid = make("ruby", name="No entry") if "ruby" in have else None
    if yid:
        a = run(yid, 10)
        check("required entrypoint missing -> error", a["status"] == "error")

print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
