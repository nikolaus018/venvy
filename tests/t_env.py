"""Lost key: env must be flagged unreadable, a save without env_vars must keep the ciphertext, start must refuse."""
import sqlite3, sys, tempfile, time
from pathlib import Path
from harness import admin, make_app, server
data = tempfile.mkdtemp(prefix="venvy-t-")
with server(sys.argv[1], 55005, data=data, keep=True) as (base, d, srv):
    cl = admin(base); app_id = make_app(cl, env_vars={"K": "v"})
raw0 = sqlite3.connect(Path(data) / "data.db").execute("select env_json from app").fetchone()[0]
(Path(data) / ".venvy_key").unlink()
with server(sys.argv[1], 55005, data=data) as (base, d, srv):
    cl = admin(base)
    a = cl.status(app_id)
    r = cl.req("PUT", f"/api/apps/{app_id}", json={"restart_policy": "never"})
    raw1 = sqlite3.connect(Path(d) / "data.db").execute("select env_json from app").fetchone()[0]
    cl.req("POST", f"/api/apps/{app_id}/control?action=start"); time.sleep(2)
    st = cl.status(app_id)["status"]; logs = cl.req("GET", f"/api/apps/{app_id}/logs").json()["content"]
    r2 = cl.req("PUT", f"/api/apps/{app_id}", json={"env_vars": {"K": "new"}})
    b = cl.status(app_id)
    print("flagged:", a.get("env_unreadable"), "| ciphertext kept:", raw1 == raw0, "| start refused:", st, "can't be decrypted" in logs,
          "| after re-entering:", b.get("env_unreadable"), b["env_json"])
    sys.exit(0 if a.get("env_unreadable") is True and raw1 == raw0 and st == "error" and "can't be decrypted" in logs and b.get("env_unreadable") is False and '"new"' in b["env_json"] else 1)
