"""scripts/ship.py: uploads tracked files, skips unchanged ones, lists removed
files as stale, and only deletes them with --prune (never .env, never --keep)."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
from harness import admin, make_app, server
repo = sys.argv[1]; fails = []
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)
with server(repo, 55031) as (base, d, srv), tempfile.TemporaryDirectory() as tmp:
    cl = admin(base)
    app_id = make_app(cl, name="Ship")
    home = Path(tmp) / "home"; (home / ".config" / "venvy").mkdir(parents=True)
    # reuse the admin's session as ship.py's saved login
    host = base.split("//")[1].replace(":", "_")
    (home / ".config" / "venvy" / f"{host}.json").write_text(json.dumps(
        {"session": cl.c.cookies.get("session"), "auth_key": cl.key}))
    proj = Path(tmp) / "proj"; proj.mkdir()
    run = lambda *a, **k: subprocess.run([sys.executable, str(Path(repo) / "scripts" / "ship.py"), "--url", base, *a],
                                         cwd=proj, env={**os.environ, "HOME": str(home)}, capture_output=True, text=True, **k)
    sh = lambda *a: subprocess.run(a, cwd=proj, check=True, capture_output=True)
    sh("git", "init", "-q")
    (proj / "pkg").mkdir(); (proj / "pkg" / "old.py").write_text("old")
    (proj / "keep.py").write_text("keep"); (proj / "big.bin").write_bytes(os.urandom(9 * 1024 * 1024))
    sh("git", "add", "-A")
    r = run("sync", str(app_id))
    check("first sync uploads 3", "3 uploaded, 0 unchanged" in r.stdout, r.stdout + r.stderr)
    tree = lambda p="": {i["path"]: i for i in cl.req("GET", f"/api/apps/{app_id}/files/tree", params={"path": p}).json()}
    check("big chunked file arrived intact", tree()["big.bin"]["size"] == 9 * 1024 * 1024)
    cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "create_file", "path": ".env"})
    cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "create_file", "path": "notes.txt"})
    (proj / "pkg" / "old.py").unlink(); sh("git", "rm", "-q", "--cached", "pkg/old.py")
    r = run("sync", str(app_id))
    check("second sync skips unchanged", "0 uploaded, 2 unchanged" in r.stdout, r.stdout)
    check("removed + untracked listed as stale", "stale  pkg/old.py" in r.stdout and "stale  notes.txt" in r.stdout, r.stdout)
    check(".env never stale", ".env" not in r.stdout.replace("--prune", ""), r.stdout)
    check("nothing deleted without --prune", "pkg" in tree() and "notes.txt" in tree())
    r = run("sync", str(app_id), "--prune", "--keep", "notes.txt")
    check("prune deletes removed file and its empty folder", "pkg" not in tree(), r.stdout + r.stderr)
    check("--keep and .env survive", "notes.txt" in tree() and ".env" in tree())
    check("dry-run changes nothing", run("sync", str(app_id), "--prune", "--dry-run").returncode == 0 and "keep.py" in tree())
    check("unknown app is an error", run("sync", "nope").returncode != 0)
    # A same-size edit is a change: by size alone it was never shipped.
    (proj / "keep.py").write_text("KEEP"); sh("git", "add", "-A")
    r = run("sync", str(app_id))
    check("same-size edit is uploaded", "upload keep.py" in r.stdout, r.stdout + r.stderr)
    got = cl.req("GET", f"/api/apps/{app_id}/files/tree", params={"hashes": "true"}).json()
    check("tree reports sha256 on request", any(i.get("sha256") for i in got if not i["is_dir"]), got)
    # What the app wrote (its SQLite database) is not the repo's to prune.
    cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "data.db", "content": "x"})
    run("sync", str(app_id), "--prune")
    check("--prune keeps *.db", "data.db" in tree())
    # A tracked symlink out of the checkout must not ship the file it points to.
    secret = Path(tmp) / "secret.txt"; secret.write_text("private")
    os.symlink(secret, proj / "link.txt"); sh("git", "add", "-A")
    r = run("sync", str(app_id))
    check("symlink out of the checkout skipped", "link.txt" not in tree() and "skip   link.txt" in r.stdout, r.stdout)
print("FAILED:" if fails else "ALL OK", fails or "")
sys.exit(1 if fails else 0)
