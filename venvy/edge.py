"""Ways in from the internet besides the Cloudflare tunnel.

* TLS with Caddy: Venvy writes Caddy's configuration so that Caddy terminates
  HTTPS for every hostname Venvy routes, getting certificates from Let's
  Encrypt / ZeroSSL on demand (it asks Venvy — /_venvy/edge-ask — whether a
  name is one of ours before requesting one, so wildcards and new aliases
  work without a reload), redirects HTTP to HTTPS, and passes everything to
  Venvy's port, where the proxy routes it. DNS for the names must point here.
* Tailscale: an app published with `tailscale serve` (inside the tailnet) or
  `tailscale funnel` (on the internet), on this machine's ts.net name.
* ngrok: an ngrok agent per published app, run and restarted by Venvy, that
  sends its domain to Venvy's proxy. The authtoken stays with Venvy.
"""
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

from venvy import kv
from venvy.config import PROXY_SECRET, venvy_port

CADDY_KEY = "edge_caddy"          # {"enabled": bool, "email": str, "venvy_hosts": [..]}
TAILSCALE_KEY = "edge_tailscale"  # {app_id: {"mode": "serve"|"funnel", "https_port": int}}
NGROK_KEY = "edge_ngrok"          # {"token": ...} (secret)
NGROK_APPS_KEY = "edge_ngrok_apps"  # {app_id: domain}
CADDYFILE = Path(os.getenv("VENVY_CADDYFILE", "/etc/caddy/Caddyfile"))
_SYS = "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin"
_HOST_RE = re.compile(r"(?=.{1,253}\Z)(\*\.)?([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z")


def _which(name: str) -> Optional[str]:
    return shutil.which(name, path=_SYS)


# ---------------------------------------------------------------------------
# Caddy
# ---------------------------------------------------------------------------

def caddy_settings() -> dict:
    return kv.get_json(CADDY_KEY, {"enabled": False, "email": "", "venvy_hosts": []}) or {}


def caddyfile_text(cfg: dict) -> str:
    port = venvy_port()
    email = (cfg.get("email") or "").strip()
    secret_line = f"\n\t\theader_up X-Venvy-Proxy-Secret {json.dumps(PROXY_SECRET)}" if PROXY_SECRET else ""
    glob = "{\n"
    if email:
        glob += f"\temail {email}\n"
    glob += f"\ton_demand_tls {{\n\t\task http://127.0.0.1:{port}/_venvy/edge-ask\n\t}}\n}}\n"
    site = (f"https:// {{\n\ttls {{\n\t\ton_demand\n\t}}\n\tencode zstd gzip\n"
            f"\treverse_proxy 127.0.0.1:{port} {{\n\t\theader_up CF-Connecting-IP {{remote_host}}"
            f"\n\t\theader_up X-Forwarded-Proto https{secret_line}\n\t}}\n}}\n")
    redirect = "http:// {\n\tredir https://{host}{uri} 308\n}\n"
    return "# Managed by Venvy (System -> Edge). Edits here are overwritten.\n" + glob + "\n" + site + "\n" + redirect


def caddy_status() -> dict:
    cfg = caddy_settings()
    have = bool(_which("caddy"))
    active = False
    if have:
        with contextlib.suppress(Exception):
            r = subprocess.run([_which("systemctl") or "systemctl", "is-active", "caddy"], capture_output=True,
                               text=True, timeout=10)
            active = r.stdout.strip() == "active"
    return {"installed": have, "running": active, "enabled": bool(cfg.get("enabled")), "email": cfg.get("email", ""),
            "venvy_hosts": cfg.get("venvy_hosts", []), "caddyfile": str(CADDYFILE)}


async def apply_caddy(cfg: dict) -> dict:
    """Write the Caddyfile, validate it, reload Caddy. Raises RuntimeError."""
    caddy = _which("caddy")
    if not caddy:
        raise RuntimeError("Caddy is not installed (installer: --with-caddy).")
    hosts = [h.strip().lower() for h in cfg.get("venvy_hosts", []) if h.strip()]
    for h in hosts:
        if not _HOST_RE.fullmatch(h):
            raise RuntimeError(f"'{h}' is not a hostname.")
    email = (cfg.get("email") or "").strip()
    if email and not re.fullmatch(r"[^@\s{}]+@[^@\s{}]+\.[^@\s{}]+", email):
        raise RuntimeError("That is not an email address.")
    cfg = {"enabled": bool(cfg.get("enabled")), "email": email, "venvy_hosts": hosts}
    kv.put_json(CADDY_KEY, cfg)
    if not cfg["enabled"]:
        return caddy_status()
    text = caddyfile_text(cfg)
    tmp = CADDYFILE.with_name(".Caddyfile.venvy")
    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text)
    r = await asyncio.to_thread(subprocess.run, [caddy, "validate", "--adapter", "caddyfile", "--config", str(tmp)],
                                capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("Caddy did not accept the configuration: " + (r.stderr or r.stdout)[-500:])
    if CADDYFILE.exists() and not CADDYFILE.with_name("Caddyfile.before-venvy").exists():
        with contextlib.suppress(OSError):
            shutil.copy2(CADDYFILE, CADDYFILE.with_name("Caddyfile.before-venvy"))
    os.replace(tmp, CADDYFILE)
    sc = _which("systemctl")
    if sc:
        await asyncio.to_thread(subprocess.run, [sc, "reload-or-restart", "caddy"], capture_output=True, timeout=60)
    return caddy_status()


def ask(domain: str) -> bool:
    """Whether Caddy may get a certificate for *domain*."""
    from venvy import routing
    d = (domain or "").strip().lower().rstrip(".")
    if not d or not _HOST_RE.fullmatch(d) or d.startswith("*."):
        return False
    cfg = caddy_settings()
    if not cfg.get("enabled"):
        return False
    if d in [h.lower() for h in cfg.get("venvy_hosts", [])]:
        return True
    routing._build()
    return routing.match_sync(d, "/") is not None


# ---------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------

FUNNEL_PORTS = (443, 8443, 10000)


async def _ts(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    ts = _which("tailscale")
    if not ts:
        raise RuntimeError("Tailscale is not installed on this host.")
    return await asyncio.to_thread(subprocess.run, [ts, *args], capture_output=True, text=True, timeout=timeout)


async def tailscale_status() -> dict:
    if not _which("tailscale"):
        return {"installed": False}
    r = await _ts("status", "--json")
    try:
        st = json.loads(r.stdout or "{}")
    except ValueError:
        st = {}
    me = st.get("Self") or {}
    return {"installed": True, "backend": st.get("BackendState"), "dns_name": (me.get("DNSName") or "").rstrip("."),
            "published": kv.get_json(TAILSCALE_KEY, {}) or {}}


async def tailscale_publish(app_id: int, port: int, mode: str, https_port: int) -> str:
    if mode not in ("serve", "funnel"):
        raise RuntimeError("mode must be serve or funnel.")
    if mode == "funnel" and https_port not in FUNNEL_PORTS:
        raise RuntimeError("Funnel only works on ports 443, 8443 and 10000.")
    if not 1 <= https_port <= 65535:
        raise RuntimeError("Invalid port.")
    pub = kv.get_json(TAILSCALE_KEY, {}) or {}
    for other, v in pub.items():
        if int(other) != app_id and int(v.get("https_port", 0)) == https_port:
            raise RuntimeError(f"Port {https_port} is already used for another app.")
    r = await _ts(mode, "--bg", f"--https={https_port}", f"http://127.0.0.1:{port}")
    if r.returncode != 0:
        raise RuntimeError(f"tailscale {mode} failed: " + (r.stderr or r.stdout).strip()[-400:])
    pub[str(app_id)] = {"mode": mode, "https_port": https_port, "port": port}
    kv.put_json(TAILSCALE_KEY, pub)
    st = await tailscale_status()
    name = st.get("dns_name") or "this-machine.ts.net"
    return f"https://{name}" + ("" if https_port == 443 else f":{https_port}")


async def tailscale_unpublish(app_id: int) -> None:
    pub = kv.get_json(TAILSCALE_KEY, {}) or {}
    v = pub.pop(str(app_id), None)
    if v:
        with contextlib.suppress(Exception):
            await _ts(v["mode"], f"--https={v['https_port']}", "off")
    kv.put_json(TAILSCALE_KEY, pub)


async def tailscale_follow_ports() -> None:
    """Re-point published apps whose port changed."""
    from sqlmodel import Session
    from venvy.db import engine
    from venvy.models import App
    pub = kv.get_json(TAILSCALE_KEY, {}) or {}
    for aid, v in list(pub.items()):
        with Session(engine) as s:
            a = s.get(App, int(aid))
        if a is None:
            await tailscale_unpublish(int(aid))
        elif a.port and a.port != v.get("port"):
            with contextlib.suppress(Exception):
                await tailscale_publish(int(aid), a.port, v["mode"], int(v["https_port"]))


# ---------------------------------------------------------------------------
# ngrok
# ---------------------------------------------------------------------------

_ngrok: Dict[int, asyncio.subprocess.Process] = {}
_ngrok_state: Dict[int, dict] = {}


def ngrok_apps() -> Dict[str, str]:
    return kv.get_json(NGROK_APPS_KEY, {}) or {}


def ngrok_token() -> str:
    return (kv.get_json(NGROK_KEY, {}, secret=True) or {}).get("token", "")


async def _run_ngrok(app_id: int, domain: str) -> None:
    ng = _which("ngrok")
    token = ngrok_token()
    if not ng or not token:
        _ngrok_state[app_id] = {"running": False, "error": "ngrok is not installed" if not ng else "no authtoken set"}
        return
    env = {"PATH": _SYS, "HOME": "/nonexistent", "NGROK_AUTHTOKEN": token}
    argv = [ng, "http", f"http://127.0.0.1:{venvy_port()}", f"--url=https://{domain}", f"--host-header={domain}",
            "--log=stdout", "--log-format=logfmt"]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                env=env, start_new_session=True)
    _ngrok[app_id] = proc
    _ngrok_state[app_id] = {"running": True, "error": None, "since": time.time(), "url": f"https://{domain}"}
    last = ""
    assert proc.stdout is not None
    async for line in proc.stdout:
        t = line.decode(errors="replace").strip()
        if "lvl=eror" in t or "err=" in t:
            last = t[-300:]
    await proc.wait()
    _ngrok.pop(app_id, None)
    _ngrok_state[app_id] = {"running": False, "error": last or f"ngrok exited ({proc.returncode})"}


async def ngrok_task() -> None:
    """Keep one agent running per published app (restart with back-off)."""
    tasks: Dict[int, asyncio.Task] = {}
    backoff: Dict[int, float] = {}
    try:
        while True:
            want = {int(k): v for k, v in ngrok_apps().items()}
            for aid in [a for a in tasks if a not in want]:
                p = _ngrok.get(aid)
                if p is not None and p.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        p.terminate()
                tasks.pop(aid).cancel()
                _ngrok_state.pop(aid, None)
            for aid, dom in want.items():
                t = tasks.get(aid)
                if t is None or t.done():
                    if time.monotonic() < backoff.get(aid, 0):
                        continue
                    backoff[aid] = time.monotonic() + 15
                    tasks[aid] = asyncio.ensure_future(_run_ngrok(aid, dom))
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        for p in _ngrok.values():
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
        raise


def ngrok_status() -> dict:
    return {"installed": bool(_which("ngrok")), "has_token": bool(ngrok_token()),
            "apps": {k: {"domain": v, **_ngrok_state.get(int(k), {})} for k, v in ngrok_apps().items()}}
