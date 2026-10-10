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
UI (System → Database Backups), or hit the API directly:
  - `POST /api/system/backup` bundles a consistent `data.db` snapshot (via
    `VACUUM INTO` — safe on a live WAL-mode database, unlike a raw `cp`),
    `.venvy_key`, and all of `apps/` into one `full-*.tar.gz` in `backups/`.
    One file is a complete backup; no shell access to the server needed.
  - `POST /api/system/backup/restore/{filename}` rolls this same live
    server back to one of those archives in place: stops every running
    app, swaps in the backup's files, and restarts the process. A safety
    backup of the pre-restore state is taken automatically first.

Disaster case — the host itself is gone and you're standing up a
replacement, so there's no running Venvy to call the API on: this still
requires manual file placement, same as always —
  1. Stop Venvy on the new host (if it's already running against a fresh DB).
  2. Untar a `full-*.tar.gz` backup (or place a manually-copied `data.db` +
     `.venvy_key` + `apps/`) into the new working directory.
  3. Start Venvy — it picks up the restored files.
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

from venvy.app import app
from venvy.config import SESSION_COOKIE_SECURE
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
    {gray}Premium Application Manager — v4.0.0{reset}
    
    {indigo}➜{reset}  {bold}Local:{reset}    http://127.0.0.1:{port}
    {indigo}➜{reset}  {bold}Network:{reset}  http://{host}:{port}
    {gray}──────────────────────────────────────────────────{reset}
    """
    print(banner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Venvy Backend Server")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"), help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "54783")), help="Port to bind to")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"), help="Logging level")
    
    args = parser.parse_args()

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