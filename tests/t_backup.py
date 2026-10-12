"""Backup/restore status is the server's, not one browser's: a second login sees
a running backup; an interrupted one is reported as such after a restart; a
backup uploaded from elsewhere is checked, listed and restorable."""
import json, sys, tempfile, time
from pathlib import Path
import httpx
from harness import admin, make_app, server

repo = sys.argv[1]; fails = []
def check(n, c, x=""):
    print(("  ok   " if c else "  FAIL ") + n, x if not c else "")
    if not c: fails.append(n)

def op(cl):
    return cl.req("GET", "/api/system/backup/status").json()["operation"]

def upload(cl, blob, name, size=None, piece=None):
    """Send *blob* in chunks; returns the last response."""
    size = size or len(blob); piece = piece or max(1, len(blob) // 3 + 1); off = 0
    while True:
        r = cl.req("POST", "/api/system/backups/upload/chunk", files={"chunk": ("chunk", blob[off:off + piece])},
                   data={"upload_id": "ab" * 16, "filename": name, "offset": str(off), "total": str(size)})
        if r.status_code != 200 or r.json()["done"]:
            return r
        off = r.json()["received"]

# A status file left behind by a process that died mid-backup.
data = Path(tempfile.mkdtemp(prefix="venvy-t-"))
(data / "backups").mkdir()
(data / "backups" / ".operation.json").write_text(json.dumps({
    "id": "dead", "kind": "backup", "status": "running", "step": 2, "detail": "x", "percent": 40,
    "started_at": "2026-01-01T00:00:00+00:00", "finished_at": None, "started_by": "admin",
    "filename": None, "size_bytes": None, "error": None}))
(data / "backups" / ".tmp-20260101-000000-dead.tar.gz").write_bytes(b"half an archive")

with server(repo, 55011, data=data) as (base, data, srv):
    cl = admin(base)
    o = op(cl)
    check("interrupted backup reported as failed", o["status"] == "error" and "did not finish" in o["error"], str(o))
    check("its temp file is gone", not list((data / "backups").glob(".tmp-*")))

    app_id = make_app(cl)
    st = cl.status(app_id)
    check("new app has no resource limits", st["memory_limit_mb"] is None and st["cpu_limit_percent"] is None, str(st))
    # enough bytes that archiving is observable
    cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "marker.txt", "content": "before"})

    r = cl.req("POST", "/api/system/backup")
    r2 = cl.req("POST", "/api/system/backup")
    check("second backup while one runs -> 409", r.status_code == 200 and r2.status_code == 409, f"{r.status_code} {r2.status_code}")
    other = admin(base)   # "another browser": a separate session that never saw the job id
    seen = op(other)
    check("another session sees the backup", seen and seen["kind"] == "backup" and seen["started_by"] == "admin"
          and len(seen["steps"]) == 4 and 0 <= seen["step"] < 4, str(seen))
    res = cl.job(r)
    done = op(other)
    check("status ends as done with the file", done["status"] == "done" and done["filename"] == Path(res["path"]).name
          and done["size_bytes"] == res["size_bytes"] and done["finished_at"], str(done))
    check("a new backup can start afterwards", cl.req("GET", "/api/system/backups").json()[0]["filename"] == done["filename"])

    # take it "off site", delete it here, bring it back
    name = done["filename"]
    blob = cl.req("GET", f"/api/system/backups/{name}/download").content
    cl.req("DELETE", f"/api/system/backups/{name}")
    check("list empty after delete", cl.req("GET", "/api/system/backups").json() == [])

    r = upload(cl, b"this is not a backup at all" * 50, "full-20200101-000000.tar.gz")
    check("junk upload refused", r.status_code == 400 and "not a Venvy backup" in r.json()["detail"], r.text)
    check("junk left nothing behind", not list((data / "backups").glob(".upload-*")) and cl.req("GET", "/api/system/backups").json() == [])
    r = cl.req("POST", "/api/system/backups/upload/chunk", files={"chunk": ("chunk", b"zz")},
               data={"upload_id": "cd" * 16, "filename": name, "offset": "5", "total": "100"})
    check("wrong offset -> 409 with the server's count", r.status_code == 409 and r.json()["detail"]["received"] == 0, r.text)
    r = cl.req("POST", "/api/system/backups/upload/chunk", files={"chunk": ("chunk", b"zz")},
               data={"upload_id": "../../x", "filename": name, "offset": "0", "total": "100"})
    check("bad upload id refused", r.status_code == 400, r.text)

    r = upload(cl, blob, name)
    lst = cl.req("GET", "/api/system/backups").json()
    check("uploaded backup is listed under its name", r.status_code == 200 and r.json()["filename"] == name
          and [b["filename"] for b in lst] == [name] and lst[0]["size_bytes"] == len(blob), r.text)
    r = upload(cl, blob, "my backup (1).tar.gz")
    check("renamed upload gets a proper name", r.status_code == 200 and r.json()["filename"] != name
          and r.json()["filename"].startswith("full-"), r.text)
    cl.req("DELETE", f"/api/system/backups/{r.json()['filename']}")

    # restore the uploaded one and follow it through the restart
    cl.req("POST", f"/api/apps/{app_id}/files/op", json={"operation": "save", "path": "marker.txt", "content": "after"})
    r = cl.req("POST", f"/api/system/backup/restore/{name}", json={"confirm": "RESTORE"})
    check("backup refused while restoring", cl.req("POST", "/api/system/backup").status_code == 409)
    steps_seen = set()
    end = time.time() + 120; final = None
    while time.time() < end:
        try:
            o = op(cl)   # not `other`: that login is newer than the backup, so the restore removes it
        except Exception:
            time.sleep(0.2); continue    # restarting
        if o and o["kind"] == "restore":
            steps_seen.add(o["step"])
            if o["status"] != "running":
                final = o; break
        time.sleep(0.05)
    check("restore reports done after the restart", final and final["status"] == "done" and final["filename"] == name, str(final))
    check("restore reached its last step", 4 in steps_seen and len(final["steps"]) == 5, str(steps_seen))
    for _ in range(60):
        try:
            if httpx.get(base + "/api/health", timeout=1).status_code == 200: break
        except Exception: pass
        time.sleep(0.5)
    got = cl.req("GET", f"/api/apps/{app_id}/files/download", params={"path": "marker.txt"}).text
    check("restore of the uploaded backup rolled the file back", got == "before", got)

print("FAILED" if fails else "ALL OK", fails); sys.exit(1 if fails else 0)
