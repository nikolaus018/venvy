"""
Venvy backend — dependency check.
If a required package is missing, we print a clear error instead of
silently installing at runtime (which can corrupt a running venv and
blocks the event loop on startup).

-------------------------------------------------------------------------------
Backup & restore procedure
-------------------------------------------------------------------------------
Venvy's persistent state lives under the directory Venvy is launched from
(DATA_DIR = the current working directory at startup): `data.db` (SQLite —
users, sessions, app metadata, settings), `apps/` (every hosted app's code,
venv, and logs), and `.venvy_key` (the local symmetric-encryption key used
to decrypt Cloudflare credentials and app env vars stored in `data.db` —
without it, those encrypted columns are unrecoverable; treat it like a
private key).

Normal case — Venvy is alive, you want a backup or to roll back: use the
UI (System → Backups), or hit the API directly:
  - `POST /api/system/backup` bundles a consistent `data.db` snapshot (via
    `VACUUM INTO` — safe on a live WAL-mode database, unlike a raw `cp`),
    `.venvy_key`, and all of `apps/` into one `full-*.tar.gz` in `backups/`.
    One file is a complete backup; no shell access to the server needed.
  - `POST /api/system/backup/restore/{filename}` rolls this same live
    server back to one of those archives in place: stops every running
    app, swaps in the backup's files, and restarts the process. A safety
    backup of the pre-restore state is taken automatically first.
  - `GET /api/system/backup/status` says what either of those is doing
    right now, whoever started it and whether or not their browser is
    still open.

Disaster case — the host itself is gone and you're standing up a
replacement: install Venvy on the new host, create the first admin, then
System → Backups → Upload Backup with the `full-*.tar.gz` you kept
elsewhere, and restore it. (You will be signed out: the users and sessions
are the backup's.) By hand, without the UI:
  1. Stop Venvy on the new host.
  2. Untar a `full-*.tar.gz` backup (or place a manually-copied `data.db` +
     `.venvy_key` + `apps/`) into the new working directory.
  3. Start Venvy — it picks up the restored files.

Locked out — the only admin forgot the password or lost the authenticator:
from Venvy's data directory, as the user the service runs as,
  python main.py --reset-password USERNAME
  python main.py --disable-2fa USERNAME
change the account directly in data.db (the server may keep running) and sign
that user out everywhere.
-------------------------------------------------------------------------------
"""
import os as _os; _os.environ.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")

import sys as _bootstrap_sys

_REQUIRED = ['cryptography', 'aiofiles', 'uvicorn', 'fastapi', 'sqlmodel', 'aiohttp', 'multipart']
if not getattr(_bootstrap_sys, 'frozen', False):
    _missing = [m for m in _REQUIRED if not __import__('importlib.util', fromlist=['']).find_spec(m)]
    if _missing:
        print(
            f"[Venvy] Missing packages: {', '.join(_missing)}\n"
            f"  Run: {_bootstrap_sys.executable} -m pip install {' '.join(_missing)}",
            file=_bootstrap_sys.stderr,
        )
        _bootstrap_sys.exit(1)

import argparse
import logging
import os
import sys

import uvicorn

from venvy import __version__
from venvy.app import app
from venvy.config import DB_FILE, SESSION_COOKIE_SECURE
from venvy.process import get_server_ipv4


def _print_banner(host: str, port: int):
    """Print a stylized Venvy ASCII banner on startup."""
    indigo = "\033[38;5;63m"
    gray = "\033[90m"
    bold = "\033[1m"
    reset = "\033[0m"
    
    banner = f"""
{indigo}{bold}    ██╗   ██╗███████╗███╗   ██╗██╗   ██╗██╗   ██╗
    ██║   ██║██╔════╝████╗  ██║██║   ██║╚██╗ ██╔╝
    ██║   ██║█████╗  ██╔██╗ ██║██║   ██║ ╚████╔╝ 
    ╚██╗ ██╔╝██╔══╝  ██║╚██╗██║╚██╗ ██╔╝  ╚██╔╝  
     ╚████╔╝ ███████╗██║ ╚████║ ╚████╔╝    ██║   
      ╚═══╝  ╚══════╝╚═╝  ╚═══╝  ╚═══╝     ╚═╝   {reset}
    {gray}Application Manager — v{__version__}{reset}
    
    {indigo}➜{reset}  {bold}Local:{reset}    http://127.0.0.1:{port}
    {indigo}➜{reset}  {bold}Network:{reset}  http://{host}:{port}
    {gray}──────────────────────────────────────────────────{reset}
    """
    print(banner)


def _recover_account(username: str, reset_password: bool, disable_2fa: bool) -> int:
    """Account recovery from the command line, for when nobody can sign in to
    do it from the UI. Works on data.db in the current directory, whether or
    not the server is running. Returns the process exit code."""
    import getpass

    from fastapi import HTTPException
    from sqlmodel import Session, select

    from venvy.auth import _validate_password_strength, hash_password
    from venvy.db import engine
    from venvy.models import User, UserSession

    if not DB_FILE.exists():
        print(f"No data.db in {DB_FILE.parent} — run this from Venvy's data directory "
              "(/var/lib/venvy for an installed service).", file=sys.stderr)
        return 1
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user:
            names = ", ".join(sorted(u.username for u in session.exec(select(User)).all())) or "none"
            print(f"No user named {username!r}. Existing users: {names}", file=sys.stderr)
            return 1
        if reset_password:
            password = getpass.getpass(f"New password for {username}: ")
            try:
                _validate_password_strength(password)
            except HTTPException as exc:
                print(exc.detail, file=sys.stderr)
                return 1
            if getpass.getpass("Repeat it: ") != password:
                print("The two passwords are not the same. Nothing was changed.", file=sys.stderr)
                return 1
            user.password_hash = hash_password(password)
        if disable_2fa:
            user.totp_enabled = False
            user.totp_secret = None
            user.totp_last_counter = None
        session.add(user)
        for row in session.exec(select(UserSession).where(UserSession.user_id == user.id)).all():
            session.delete(row)
        session.commit()
    done = " and ".join(what for what, on in (("password changed", reset_password),
                                              ("two-factor authentication switched off", disable_2fa)) if on)
    print(f"{username}: {done}. Signed out everywhere.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Venvy Backend Server")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "54783")), help="Port to bind to")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"), help="Logging level")
    parser.add_argument("--version", action="version", version=f"Venvy {__version__}")
    recovery = parser.add_argument_group(
        "account recovery", "run from the data directory instead of starting the server")
    recovery.add_argument("--reset-password", metavar="USERNAME", help="Set a new password for a user")
    recovery.add_argument("--disable-2fa", metavar="USERNAME", help="Switch two-factor authentication off for a user")

    args = parser.parse_args()

    if args.reset_password or args.disable_2fa:
        if args.reset_password and args.disable_2fa and args.reset_password != args.disable_2fa:
            parser.error("--reset-password and --disable-2fa must name the same user when given together")
        sys.exit(_recover_account(args.reset_password or args.disable_2fa,
                                  bool(args.reset_password), bool(args.disable_2fa)))

    _print_banner(args.host if args.host != "0.0.0.0" else get_server_ipv4(), args.port)
    import importlib.util as _ilu
    if not (_ilu.find_spec("websockets") or _ilu.find_spec("wsproto")):
        logging.warning(
            "No WebSocket library installed — the in-browser terminal won't connect. "
            "Install it with: %s -m pip install 'uvicorn[standard]'", sys.executable)
    if args.host not in ("127.0.0.1", "localhost", "::1") and not SESSION_COOKIE_SECURE:
        logging.warning(
            "Venvy is listening on %s over plain HTTP: passwords and session cookies "
            "cross the network unencrypted. Prefer binding to 127.0.0.1 and reaching it "
            "through the Cloudflare tunnel (HTTPS), then set SESSION_COOKIE_SECURE=1.", args.host)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level.lower(),
        # Never let X-Forwarded-For rewrite the client address — see
        # _resolve_client_ip() for how tunnel traffic is attributed instead.
        proxy_headers=False,
        access_log=True,
    )