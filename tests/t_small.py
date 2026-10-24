import concurrent.futures as cf, shutil, sys, time
import httpx
from harness import Client, admin, make_app, server, setup_code
repo = sys.argv[1]; fails = []
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)
with server(repo, 55007) as (base, d, srv):
    # setup race: 8 simultaneous first-run setups
    def setup(i):
        c = Client(base); c.c.get("/")
        return c.req("POST", "/api/auth/setup", json={"username": f"admin{i}", "password": "hunter2hunter2", "setup_code": setup_code(base)}).status_code
    with cf.ThreadPoolExecutor(8) as ex: codes = list(ex.map(setup, range(8)))
    check("only one setup wins", codes.count(200) == 1, str(codes))
    win = codes.index(200)
    cl = Client(base); cl.c.get("/")
    cl.key = cl.req("POST", "/api/auth/login", json={"username": f"admin{win}", "password": "hunter2hunter2"}).json()["auth_key"]
    check("one user exists", len(cl.req("GET", "/api/users").json()) == 1)
    check("blank username refused", cl.req("POST", "/api/users", json={"username": "   ", "password": "abcdefg12"}).status_code == 400)
    check("username trimmed", cl.req("POST", "/api/users", json={"username": "  bob ", "password": "abcdefg12"}).status_code == 200
          and "bob" in [u["username"] for u in cl.req("GET", "/api/users").json()])
    check("blank app name refused", cl.req("POST", "/api/apps/upload", data={"name": "  ", "runtime": "python"}).status_code == 400)
    check("long app name refused", cl.req("POST", "/api/apps/upload", data={"name": "x" * 51, "runtime": "python"}).status_code == 400)
    app_id = make_app(cl, code="import time\nwhile True:\n    sum(range(200000)); time.sleep(0.01)\n")
    up = lambda **b: cl.req("PUT", f"/api/apps/{app_id}", json=b)
    check("bad env name refused", up(env_vars={"A=B": "x"}).status_code == 400 and up(env_vars={"": "x"}).status_code == 400)
    check("good env accepted", up(env_vars={"GOOD_1": "x=y"}).status_code == 200)
    up(runtime="rust")
    r = up(runtime="python", entrypoint="server.py")
    check("runtime+entrypoint in one request", r.json()["entrypoint"] == "server.py", r.text)
    up(entrypoint="main.py")
    cl.req("POST", f"/api/apps/{app_id}/control?action=start"); cl.wait_status(app_id, "running")
    time.sleep(7)
    st = cl.req("GET", f"/api/apps/{app_id}/stats").json()
    check("stats report cpu/memory/uptime", st["cpu"] > 0 and st["memory"] > 1 and st["uptime"] > 5, str(st))
    cl.req("POST", f"/api/apps/{app_id}/control?action=stop"); cl.wait_status(app_id, "stopped")
    check("stats zero when stopped", cl.req("GET", f"/api/apps/{app_id}/stats").json() == {"cpu": 0.0, "memory": 0.0, "uptime": 0.0})
    for bad in ["foo.com&type=A", "foo.com/path", "no-dot", "a b.com", "-x.com", "x..com", ""]:
        r = cl.req("POST", f"/api/apps/{app_id}/tunnel/publish", json={"hostname": bad, "local_port": 8000})
        check(f"hostname {bad!r} refused", r.status_code == 400 and "hostname" in r.text.lower(), r.text)
    r = cl.req("POST", f"/api/apps/{app_id}/tunnel/publish", json={"hostname": "https://App.Example.com/", "local_port": 8000})
    check("valid hostname passes validation", r.status_code == 400 and "not configured" in r.text, r.text)
    r = cl.req("POST", f"/api/apps/{app_id}/tunnel/publish", json={"hostname": "app.example.com", "local_port": 70000})
    check("bad port refused", r.status_code == 400 and "port" in r.text.lower(), r.text)
    # Node.js runtime (only where node/npm exist)
    check("unknown runtime refused", cl.req("POST", "/api/apps/upload", data={"name": "Bad RT", "runtime": "cobol"}).status_code == 400)
    if shutil.which("node") and shutil.which("npm"):
        r = cl.req("POST", "/api/apps/upload", data={"name": "Node App", "runtime": "node", "entrypoint": "server.js"})
        node_id = r.json()["app_id"]; cl.job(r)
        save = lambda path, content: cl.req("POST", f"/api/apps/{node_id}/files/op", json={"operation": "save", "path": path, "content": content})
        save("package.json", '{"name": "t", "dependencies": {"left-pad": "^1.3.0"}, "devDependencies": {"@scope/x": "1"}}')
        save("server.js", 'console.log("NODE-MARKER " + process.env.PORT); setInterval(() => {}, 1000);')
        check("node packages listed from package.json",
              cl.req("GET", f"/api/apps/{node_id}/packages").json() == [{"name": "left-pad", "version": "^1.3.0"}, {"name": "@scope/x", "version": "1"}])
        for bad in ["--foo", "-g", "a b", "../x", "x;rm"]:
            check(f"npm name {bad!r} refused", cl.req("POST", f"/api/apps/{node_id}/packages", json={"name": bad}).status_code == 400)
        check("npm legacy upper-case name accepted", cl.req("POST", f"/api/apps/{node_id}/packages", json={"name": "JSONStream"}).status_code == 200)
        check("npm scoped name accepted", cl.req("POST", f"/api/apps/{node_id}/packages", json={"name": "@scope/pkg@1.2.3"}).status_code == 200)
        save("package.json", '{"name": "t"}')   # the listed packages above do not exist; the first start installs what is listed
        cl.req("POST", f"/api/apps/{node_id}/control?action=start"); cl.wait_status(node_id, "running")
        time.sleep(1)
        check("node app runs its entrypoint", "NODE-MARKER" in cl.req("GET", f"/api/apps/{node_id}/logs").text)
        cl.req("POST", f"/api/apps/{node_id}/control?action=stop"); cl.wait_status(node_id, "stopped")
        check("blank entrypoint allowed for node", cl.req("PUT", f"/api/apps/{node_id}", json={"entrypoint": ""}).status_code == 200)
    check("sw.js served", httpx.get(base + "/sw.js").status_code == 200)
print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
