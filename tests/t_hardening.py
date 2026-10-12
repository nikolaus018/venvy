"""Limits and races: oversized bodies, password prompts, delete while busy,
apps orphaned by a killed server, backups of a tree that changes underneath,
and archives that try to write through a link."""
import asyncio, io, os, signal, sys, tarfile, tempfile, time
from pathlib import Path
import httpx, websockets
from harness import Client, admin, app_procs, make_app, server
repo = sys.argv[1]; fails = []
PORT = 55012
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

def upload_backup(cl, name, blob, upload_id):
    return cl.req("POST", "/api/system/backups/upload/chunk", files={"chunk": ("chunk", blob)},
                  data={"upload_id": upload_id, "filename": name, "offset": "0", "total": str(len(blob))})

def crafted_archive():
    """data.db, then a symlink pointing out of the archive and a file "inside" it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        db = tarfile.TarInfo("data.db"); db.size = 4; tar.addfile(db, io.BytesIO(b"nope"))
        d = tarfile.TarInfo("apps"); d.type = tarfile.DIRTYPE; d.mode = 0o755; tar.addfile(d)
        ln = tarfile.TarInfo("apps/out"); ln.type = tarfile.SYMTYPE; ln.linkname = "/tmp"; tar.addfile(ln)
        f = tarfile.TarInfo("apps/out/venvy-escaped.txt"); f.size = 2; tar.addfile(f, io.BytesIO(b"hi"))
    return buf.getvalue()

async def terminal_closed_by_delete(cl, base, app_id):
    cookie = "; ".join(f"{k}={v}" for k, v in cl.c.cookies.items())
    ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/api/apps/{app_id}/terminal",
                                  additional_headers={"Cookie": cookie, "Origin": base})
    await ws.send("\x01" + cl.key)
    await ws.send("echo ready-$((40+2))\n")
    buf = ""; end = time.time() + 6
    while time.time() < end and "ready-42" not in buf:
        try: buf += await asyncio.wait_for(ws.recv(), 1)
        except asyncio.TimeoutError: pass
    r = await asyncio.to_thread(cl.req, "DELETE", f"/api/apps/{app_id}")
    closed = False; end = time.time() + 5
    while time.time() < end:
        try: await asyncio.wait_for(ws.recv(), 1)
        except asyncio.TimeoutError: continue
        except websockets.ConnectionClosed: closed = True; break
    return "ready-42" in buf, r, closed

data = tempfile.mkdtemp(prefix="venvy-t-")
with server(repo, PORT, data=data, keep=True) as (base, d, srv):
    cl = admin(base)
    raw = httpx.Client(base_url=base, timeout=30); raw.get("/")
    h = {"x-csrf-token": raw.cookies.get("csrf_token"), "content-type": "application/json"}

    # ── request bodies ──
    r = raw.post("/api/auth/login", content=b"x" * (17 * 1024 * 1024), headers=h)
    check("oversized body refused before login", r.status_code == 413, f"{r.status_code} {r.text[:80]}")
    try: code = raw.post("/api/auth/login", content=(b"y" * 1048576 for _ in range(18)), headers=h).status_code
    except httpx.HTTPError: code = 0
    check("oversized chunked body cut off", code in (0, 400, 413), str(code))
    r = cl.req("GET", "/api/apps")
    check("security headers", r.headers.get("x-xss-protection") == "0" and r.headers.get("x-frame-options") == "DENY"
          and "strict-transport-security" not in r.headers, str(dict(r.headers)))

    # ── sign-in details ──
    c2 = Client(base); c2.c.get("/")
    check("username with a trailing space signs in",
          c2.req("POST", "/api/auth/login", json={"username": "admin ", "password": "hunter2hunter2"}).status_code == 200)
    wrong = {"current_password": "not-it-1", "password": "newpass123", "password_confirm": "newpass123"}
    codes = [cl.req("PUT", "/api/auth/password", json=wrong).status_code for _ in range(6)]
    check("password prompt locks after 5 wrong tries", codes == [401, 401, 401, 401, 429, 429], str(codes))
    r = cl.req("PUT", "/api/auth/password", json={**wrong, "current_password": "hunter2hunter2"})
    check("…even with the right password, and says for how long", r.status_code == 429 and int(r.headers.get("retry-after", 0)) > 0, r.text)
    check("the session itself still works", cl.req("GET", "/api/apps").status_code == 200)

    # ── bulk file operations fit under the rate limit ──
    app_id = make_app(cl)
    codes = {cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "create_file", "path": f"bulk/f{i}.txt"}).status_code for i in range(150)}
    check("150 file operations in a row", codes == {200}, str(codes))

    # ── import scan ──
    files = {"helper.py": "X = 1\n", "pkg/__init__.py": "", "use.py": "import helper, pkg, os\nimport surely_not_installed_xyz\n",
             "venv/lib/fake.py": "import inside_the_venv_abc\n", "node_modules/x/y.py": "import inside_node_modules\n"}
    for path, content in files.items():
        cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": path, "content": content})
    missing = cl.req("GET", f"/api/apps/{app_id}/scan-imports").json()["missing"]
    check("scan reports only what is really missing", missing == ["surely_not_installed_xyz"], str(missing))

    # ── delete: with a terminal open, and racing a start ──
    ready, r, closed = asyncio.run(terminal_closed_by_delete(cl, base, app_id))
    check("delete with an open terminal", ready and r.status_code == 200 and r.json() == {"ok": True, "warnings": []} and closed,
          f"ready={ready} {r.status_code} {r.text} closed={closed}")
    check("its directory is gone", not any(Path(data, "apps").glob("*/t-app*")), str(list(Path(data, "apps").glob("*/*"))))
    racer = make_app(cl, name="Racer", restart_policy="always")
    cl.req("POST", f"/api/apps/{racer}/control?action=start")
    r = cl.req("DELETE", f"/api/apps/{racer}")
    time.sleep(7)   # long enough for the supervisor's restart policy to have had its turn
    check("delete racing a start leaves no process", r.status_code == 200 and app_procs(data) == []
          and all(a["id"] != racer for a in cl.req("GET", "/api/apps").json()), f"{r.status_code} {app_procs(data)}")

    # ── a crafted archive is refused before anything changes ──
    keeper = make_app(cl, name="Keeper")
    r = upload_backup(cl, "full-20200101-000001.tar.gz", crafted_archive(), "ef" * 16)
    check("crafted archive uploads", r.status_code == 200 and r.json().get("done"), r.text)
    try: cl.job(cl.req("POST", f"/api/system/backup/restore/{r.json()['filename']}", json={"confirm": "RESTORE"})); err = ""
    except RuntimeError as e: err = str(e)
    check("…and is refused on restore", "through a link" in err, err)
    check("…with nothing touched", any(a["id"] == keeper for a in cl.req("GET", "/api/apps").json())
          and not Path("/tmp/venvy-escaped.txt").exists() and not list(Path(data).glob(".apps-replaced-*")))
    check("the refused archive can be deleted again", cl.req("DELETE", f"/api/system/backups/{r.json()['filename']}").status_code == 200)

    # ── the server is killed; its app keeps running without it ──
    cl.req("POST", f"/api/apps/{keeper}/control?action=start"); cl.wait_status(keeper, "running")
    before = app_procs(data)
    srv.send_signal(signal.SIGKILL); srv.wait(10)
    check("app outlives a killed server", len(before) == 1 and app_procs(data) == before, f"{before} {app_procs(data)}")

with server(repo, PORT, data=data) as (base, d, srv):
    cl = Client(base); cl.c.get("/")
    cl.key = cl.req("POST", "/api/auth/login", json={"username": "admin", "password": "hunter2hunter2"}).json()["auth_key"]
    ok = cl.wait_status(keeper, "running", 20); now = app_procs(data)
    check("orphaned app is restarted under supervision", ok and len(now) == 1 and now != before, f"{before} -> {now}")
    time.sleep(1.5)
    log = cl.req("GET", f"/api/apps/{keeper}/logs").json()["content"]
    check("…and its output is collected again", log.count("STARTUP-MARKER") == 2, log[-300:])
    cl.req("POST", f"/api/apps/{keeper}/control?action=stop"); cl.wait_status(keeper, "stopped")

# ── backup of a tree that changes while it is read (no server needed) ──
work = Path(tempfile.mkdtemp(prefix="venvy-t-")); os.chdir(work); sys.path.insert(0, repo)
from venvy.backup import _add_tree_to_tar   # noqa: E402
tree = work / "apps"; (tree / "sub").mkdir(parents=True)
(tree / "a-shrinks.bin").write_bytes(b"A" * 100_000); (tree / "b-vanishes.txt").write_text("gone soon")
(tree / "sub" / "kept.txt").write_text("kept"); os.symlink("/etc/hostname", tree / "link"); os.mkfifo(tree / "pipe")
def track(info):
    if info.name == "apps/a-shrinks.bin":      # between "header written" and "data read"
        with open(tree / "a-shrinks.bin", "r+b") as f: f.truncate(10)
        (tree / "b-vanishes.txt").unlink()
    return info
with tarfile.open(work / "out.tar.gz", "w:gz") as tar:
    skipped = _add_tree_to_tar(tar, str(tree), "apps", track)
with tarfile.open(work / "out.tar.gz") as tar:
    names = {m.name: m for m in tar.getmembers()}
    shrunk = tar.extractfile("apps/a-shrinks.bin").read()
    kept = tar.extractfile("apps/sub/kept.txt").read()
check("a vanished file is skipped, not fatal", skipped == 1 and "apps/b-vanishes.txt" not in names, f"{skipped} {sorted(names)}")
check("a file that shrank is padded to its header size", len(shrunk) == 100_000 and shrunk[:10] == b"A" * 10 and shrunk[10:] == b"\0" * 99_990)
check("the rest of the tree is intact", kept == b"kept" and names["apps/link"].issym() and names["apps/link"].linkname == "/etc/hostname"
      and names["apps/pipe"].isfifo(), str(sorted(names)))

print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
