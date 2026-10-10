import base64, hashlib, hmac, sqlite3, struct, sys, tempfile, time
from pathlib import Path
from harness import Client, admin, server
def code(secret, t=None):
    key = base64.b32decode(secret); c = int(t or time.time()) // 30
    h = hmac.new(key, struct.pack(">Q", c), hashlib.sha1).digest(); o = h[-1] & 15
    return f"{(struct.unpack('>I', h[o:o+4])[0] & 0x7FFFFFFF) % 1000000:06d}"
LOGIN = {"username": "admin", "password": "hunter2hunter2"}
data = tempfile.mkdtemp(prefix="venvy-t-")
with server(sys.argv[1], 54997, data=data, keep=True) as (base, d, srv):
    cl = admin(base)
    sec = cl.req("GET", "/api/auth/totp/setup").json()["secret"]
    hidden = cl.req("GET", "/api/system/settings/totp_pending_1").json()
    reserved = cl.req("PUT", "/api/system/settings/totp_pending_1", json={"value": "JBSWY3DPEHPK3PXP"}).status_code
    a = cl.req("POST", "/api/auth/totp/setup/confirm", json={"totp_code": "000000" if code(sec) != "000000" else "111111"}).status_code
    while time.time() % 30 > 20: time.sleep(1)      # stay inside one step for the replay checks
    c0 = code(sec)
    b = cl.req("POST", "/api/auth/totp/setup/confirm", json={"totp_code": c0}).status_code
    again = cl.req("GET", "/api/auth/totp/setup").status_code
    cl.req("POST", "/api/auth/logout")
    c = cl.req("POST", "/api/auth/login", json=LOGIN)
    replay_setup = cl.req("POST", "/api/auth/login", json={**LOGIN, "totp_code": c0}).status_code
    nxt = code(sec, time.time() + 30)               # next step is inside the ±1 window
    e = cl.req("POST", "/api/auth/login", json={**LOGIN, "totp_code": nxt})
    cl.key = e.json().get("auth_key"); cl.req("POST", "/api/auth/logout")
    replay_login = cl.req("POST", "/api/auth/login", json={**LOGIN, "totp_code": nxt}).status_code
    raw = sqlite3.connect(Path(d) / "data.db").execute("select totp_secret from user").fetchone()[0]
    print("pending hidden:", hidden.get("value") == "" , "reserved write:", reserved, "| wrong:", a, "right:", b, "re-setup while on:", again,
          "| no code:", c.status_code, "replay of setup code:", replay_setup, "fresh code:", e.status_code, "replay of login code:", replay_login,
          "| secret encrypted:", raw.startswith("enc:v1:"))
    ok = (hidden.get("value") == "", reserved, a, b, again, c.status_code, replay_setup, e.status_code, replay_login, raw.startswith("enc:v1:")) == (True, 400, 400, 200, 400, 403, 401, 200, 401, True)
# legacy plaintext secret gets encrypted on boot and still works
con = sqlite3.connect(Path(data) / "data.db"); con.execute("update user set totp_secret=?, totp_last_counter=NULL", (sec,)); con.commit(); con.close()
with server(sys.argv[1], 54997, data=data) as (base, d, srv):
    raw = sqlite3.connect(Path(d) / "data.db").execute("select totp_secret from user").fetchone()[0]
    cl = Client(base); cl.c.get("/")
    r = cl.req("POST", "/api/auth/login", json={**LOGIN, "totp_code": code(sec)})
    print("legacy secret encrypted on boot:", raw.startswith("enc:v1:"), "| login:", r.status_code)
    ok = ok and raw.startswith("enc:v1:") and r.status_code == 200
sys.exit(0 if ok else 1)
