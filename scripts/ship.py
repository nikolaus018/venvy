#!/usr/bin/env python3
"""Ship a git checkout to an app on a Venvy instance (stdlib only).

  ship.py login  [--url URL]
  ship.py apps   [--url URL]
  ship.py sync   APP [--url URL] [--prune] [--dry-run] [--keep GLOB ...] [--restart]

`sync` uploads every file git tracks (or has staged/untracked-but-not-ignored)
into the app, chunked, skipping files whose size already matches. Files that
were removed from the repo stay on the server (Venvy's upload never deletes):
`sync` lists them as "stale", and `--prune` deletes them. `.env*` files and
anything matching --keep are never touched; folders Venvy hides from the file
tree (venv, node_modules, logs, .git, __pycache__) are never seen at all.

APP is the app's id or exact name. The session is kept in
~/.config/venvy/<host>.json (mode 0600) and renews with use; the password is
only ever typed at the prompt. The URL defaults to $VENVY_URL.
"""
import argparse
import fnmatch
import getpass
import http.cookiejar
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CHUNK = 8 * 1024 * 1024
CONFIG_DIR = Path.home() / ".config" / "venvy"
ALWAYS_KEEP = [".env", ".env.*"]


class Client:
    def __init__(self, url: str):
        self.base = url.rstrip("/")
        host = urllib.parse.urlparse(self.base).netloc.replace(":", "_")
        self.state_file = CONFIG_DIR / f"{host}.json"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.auth_key = ""

    # -- session ------------------------------------------------------------
    def load(self) -> bool:
        try:
            st = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return False
        self.auth_key = st["auth_key"]
        domain = urllib.parse.urlparse(self.base).hostname
        self.jar.set_cookie(http.cookiejar.Cookie(
            0, "session", st["session"], None, False, domain, True, False, "/", True,
            self.base.startswith("https"), None, False, None, None, {}))
        return True

    def save(self) -> None:
        session = next((c.value for c in self.jar if c.name == "session"), "")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"session": session, "auth_key": self.auth_key}, f)

    def login(self) -> None:
        user = input("Username: ")
        body = {"username": user, "password": getpass.getpass("Password: ")}
        try:
            res = self.request("POST", "/api/auth/login", body)
        except urllib.error.HTTPError as e:
            if e.code != 403 or b"totp_required" not in e.read():
                raise
            body["totp_code"] = input("Two-factor code: ")
            res = self.request("POST", "/api/auth/login", body)
        self.auth_key = res["auth_key"]
        self.save()
        print(f"Signed in as {res['username']} ({res['role']}).")

    # -- http ---------------------------------------------------------------
    def request(self, method, path, body=None, form=None):
        headers = {"X-Venvy-Key": self.auth_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            boundary = uuid.uuid4().hex
            data = b""
            for name, val in form.items():
                data += f"--{boundary}\r\n".encode()
                if isinstance(val, bytes):
                    data += (f'Content-Disposition: form-data; name="{name}"; filename="chunk"\r\n'
                             "Content-Type: application/octet-stream\r\n\r\n").encode() + val + b"\r\n"
                else:
                    data += f'Content-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode()
            data += f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        if method != "GET":
            csrf = next((c.value for c in self.jar if c.name == "csrf_token"), None)
            if csrf is None:  # the cookie is issued on any first response
                self.opener.open(self.base + "/api/health").read()
                csrf = next((c.value for c in self.jar if c.name == "csrf_token"), "")
            headers["X-CSRF-Token"] = csrf
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with self.opener.open(req) as r:
            raw = r.read()
        self.save() if self.auth_key else None  # sliding session: keep the cookie fresh
        return json.loads(raw) if raw else None

    def upload(self, app_id: int, rel: str, data: bytes) -> None:
        upload_id, offset = secrets.token_hex(16), 0
        while True:
            chunk = data[offset:offset + CHUNK]
            try:
                res = self.request("POST", f"/api/apps/{app_id}/files/upload/chunk", form={
                    "upload_id": upload_id, "path": "", "rel_path": rel,
                    "offset": str(offset), "total": str(len(data)), "chunk": chunk})
            except urllib.error.HTTPError as e:
                if e.code == 409:  # a lost response: resume from what the server has
                    detail = json.loads(e.read()).get("detail")
                    if isinstance(detail, dict):
                        offset = detail["received"]
                        continue
                raise
            offset = res["received"]
            if res.get("done") or offset >= len(data):
                return


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True, capture_output=True).stdout.decode()
    return sorted(p for p in out.split("\0") if p and (root / p).is_file())


def remote_files(c: Client, app_id: int, path: str = "") -> dict[str, int]:
    found = {}
    for item in c.request("GET", f"/api/apps/{app_id}/files/tree?path={urllib.parse.quote(path)}"):
        if item["is_dir"]:
            found.update(remote_files(c, app_id, item["path"]))
        else:
            found[item["path"]] = item["size"]
    return found


def find_app(c: Client, ref: str) -> dict:
    apps = c.request("GET", "/api/apps")
    for a in apps:
        if str(a["id"]) == ref or a["name"] == ref:
            return a
    sys.exit(f"No app '{ref}'. Known: " + ", ".join(f"{a['id']}={a['name']}" for a in apps))


def cmd_sync(c: Client, args) -> None:
    root = Path(args.dir).resolve()
    app = find_app(c, args.app)
    local = tracked_files(root)
    remote = remote_files(c, app["id"])
    keep = ALWAYS_KEEP + args.keep

    sent = skipped = 0
    for rel in local:
        data = (root / rel).read_bytes()
        if remote.get(rel) == len(data):
            skipped += 1
            continue
        print(f"  upload {rel}")
        if not args.dry_run:
            c.upload(app["id"], rel, data)
        sent += 1

    wanted = set(local)
    stale = [p for p in remote
             if p not in wanted
             and not any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(Path(p).name, g) for g in keep)]
    for rel in stale:
        if args.prune and not args.dry_run:
            c.request("POST", f"/api/apps/{app['id']}/files/op", {"operation": "delete", "path": rel})
            print(f"  delete {rel}")
        else:
            print(f"  stale  {rel}")
    if args.prune and not args.dry_run:  # drop folders the deletions emptied
        survivors = wanted | (set(remote) - set(stale))
        for d in sorted({str(p) for r in stale for p in Path(r).parents if str(p) != "."}, key=len, reverse=True):
            live = any(p.startswith(d + "/") for p in survivors)
            if not live:
                try:
                    c.request("POST", f"/api/apps/{app['id']}/files/op", {"operation": "delete", "path": d})
                except urllib.error.HTTPError:
                    pass
    print(f"{sent} uploaded, {skipped} unchanged, {len(stale)} stale"
          f"{' (run with --prune to delete them)' if stale and not args.prune else ''}.")
    if args.restart and not args.dry_run:
        c.request("POST", f"/api/apps/{app['id']}/control?action=restart")
        print("Restart queued.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.getenv("VENVY_URL"), help="Venvy base URL (default $VENVY_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("apps")
    s = sub.add_parser("sync")
    s.add_argument("app")
    s.add_argument("--dir", default=".", help="git checkout to ship (default .)")
    s.add_argument("--prune", action="store_true", help="delete server files that are no longer in the repo")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--keep", action="append", default=[], metavar="GLOB", help="never prune matching paths (repeatable)")
    s.add_argument("--restart", action="store_true")
    args = p.parse_args()
    if not args.url:
        sys.exit("Give --url or set VENVY_URL.")
    c = Client(args.url)
    if args.cmd == "login":
        return c.login()
    if not c.load():
        sys.exit("No saved session. Run: ship.py login")
    try:
        if args.cmd == "apps":
            for a in c.request("GET", "/api/apps"):
                print(f"{a['id']:>4}  {a['name']:<24} {a.get('status', '')}")
        else:
            cmd_sync(c, args)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit("Session expired. Run: ship.py login")
        sys.exit(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


if __name__ == "__main__":
    main()
