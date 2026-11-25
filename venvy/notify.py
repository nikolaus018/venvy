"""Notifications: tell someone when something needs them.

Channels (webhook, Slack, Discord, ntfy, Telegram, email) are rows in the
database with their settings encrypted; each listens to the events it names
(or all of them). `emit()` is safe to call from anywhere — a request handler,
a worker thread, the supervisor — and never raises or blocks."""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import smtplib
import ssl
import threading
import time
import urllib.parse
from email.message import EmailMessage
from typing import Dict, List, Optional

import aiohttp
from sqlmodel import Session, select

from venvy.config import HTTP_CLIENT_LIMITS, utc_now
from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.models import NotifyChannel

EVENTS: Dict[str, str] = {
    "app_crashed": "An app died unexpectedly",
    "app_crash_loop": "An app keeps crashing (restarts back off)",
    "app_unhealthy": "An app's health check keeps failing",
    "app_recovered": "An unhealthy app is healthy again",
    "deploy_ok": "A deploy succeeded",
    "deploy_failed": "A deploy failed",
    "deploy_rolled_back": "A deploy failed its health check and was rolled back",
    "backup_ok": "A scheduled backup finished",
    "backup_failed": "A backup failed",
    "job_failed": "A scheduled job failed",
    "alert_fired": "A resource alert fired",
    "alert_cleared": "A resource alert cleared",
}

KINDS = ("webhook", "slack", "discord", "ntfy", "telegram", "email")

# What each kind needs, and which of it is secret (shown as *** once saved).
FIELDS: Dict[str, dict] = {
    "webhook": {"required": ("url",), "optional": ("secret",), "secret": ("secret",)},
    "slack": {"required": ("url",), "optional": (), "secret": ("url",)},
    "discord": {"required": ("url",), "optional": (), "secret": ("url",)},
    "ntfy": {"required": ("topic",), "optional": ("url", "token"), "secret": ("token",)},
    "telegram": {"required": ("bot_token", "chat_id"), "optional": (), "secret": ("bot_token",)},
    "email": {"required": ("host", "from", "to"), "optional": ("port", "security", "username", "password"), "secret": ("password",)},
}
MASK = "***"

_loop: Optional[asyncio.AbstractEventLoop] = None
_last_sent: Dict[tuple, float] = {}
_last_sent_lock = threading.Lock()
_COOLDOWN_SEC = 20.0
_tasks: set = set()


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


# ---------------------------------------------------------------------------
# Channel settings
# ---------------------------------------------------------------------------

def load_config(ch: NotifyChannel) -> dict:
    ok, plain = _try_decrypt_secret(ch.config)
    try:
        data = json.loads(plain) if ok and plain else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


_NTFY_TOPIC_RE = re.compile(r"\A[A-Za-z0-9_.\-]{1,128}\Z")
_TG_TOKEN_RE = re.compile(r"\A\d+:[A-Za-z0-9_\-]{10,128}\Z")
_METADATA_HOSTS = {"169.254.169.254", "100.100.100.200", "metadata.google.internal", "metadata", "fd00:ec2::254"}


def _check_safe_outbound_host(host: str, field_name: str = "url") -> None:
    """Reject link-local / cloud metadata addresses (SSRF)."""
    import ipaddress
    h = (host or "").strip().lower().strip("[]")
    if not h:
        raise ValueError(f"{field_name} must include a hostname.")
    if h in _METADATA_HOSTS:
        raise ValueError(f"{field_name} cannot target a link-local or cloud metadata address.")
    try:
        ip = ipaddress.ip_address(h)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if str(ip) in _METADATA_HOSTS or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"{field_name} cannot target a link-local or cloud metadata address.")
    except ValueError as exc:
        if "cannot target" in str(exc):
            raise


def _check_safe_outbound_url(url: str, field_name: str = "url") -> None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(f"{field_name} must start with http:// or https://")
    if any(c in url for c in "\r\n\0"):
        raise ValueError(f"{field_name} contains invalid control characters.")
    parsed = urllib.parse.urlsplit(url)
    _check_safe_outbound_host(parsed.hostname or "", field_name)


def check_config(kind: str, cfg: dict) -> dict:
    """Clean and validate a channel's settings; raises ValueError with a message."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object.")
    allowed = set(FIELDS[kind]["required"]) | set(FIELDS[kind]["optional"])
    out = {}
    for k, v in cfg.items():
        if v is None or k not in allowed:
            continue
        if k == "port":
            out[k] = v
            continue
        if not isinstance(v, (str, int, float)):
            raise ValueError(f"{k} must be a string.")
        s = str(v).strip()
        if any(c in s for c in "\r\n\0"):
            raise ValueError(f"{k} must not contain line breaks or NUL characters.")
        out[k] = s
    for field in FIELDS[kind]["required"]:
        if not out.get(field):
            raise ValueError(f"{field} is required for a {kind} channel.")
    if out.get("url"):
        _check_safe_outbound_url(out["url"], "url")
    if kind == "ntfy" and not _NTFY_TOPIC_RE.fullmatch(out["topic"]):
        raise ValueError("ntfy topic must be 1–128 characters of letters, digits, '.', '_' or '-'.")
    if kind == "telegram" and not _TG_TOKEN_RE.fullmatch(out["bot_token"]):
        raise ValueError("Telegram bot_token has an invalid format.")
    if kind == "email":
        _check_safe_outbound_host(out["host"], "host")
        try:
            port = int(out.get("port") or (465 if out.get("security") == "ssl" else 587))
        except (TypeError, ValueError):
            raise ValueError("port must be a number.")
        if not (1 <= port <= 65535):
            raise ValueError("port must be between 1 and 65535.")
        out["port"] = port
        if out.get("security", "starttls") not in ("starttls", "ssl", "none"):
            raise ValueError("security must be starttls, ssl or none.")
        out.setdefault("security", "starttls")
    return out


def masked(kind: str, cfg: dict) -> dict:
    secret = FIELDS[kind]["secret"]
    return {k: (MASK if k in secret and v else v) for k, v in cfg.items()}


def _url_origin(url: str) -> tuple:
    p = urllib.parse.urlparse((url or "").strip())
    return (p.scheme.lower(), p.netloc.lower())


def _same_secret_dest(kind: str, new: dict, old: dict) -> bool:
    """True if reusing masked secrets for *kind* sends them to the same host/port/security level."""
    if kind == "webhook":
        return _url_origin(new.get("url", "")) == _url_origin(old.get("url", ""))
    if kind == "ntfy":
        return _url_origin(new.get("url") or "https://ntfy.sh") == _url_origin(old.get("url") or "https://ntfy.sh")
    if kind == "email":
        same_host = (new.get("host") or "").strip().lower() == (old.get("host") or "").strip().lower()
        old_sec = (old.get("security") or "starttls").lower()
        new_sec = (new.get("security") or "starttls").lower()
        old_port = int(old.get("port") or (465 if old_sec == "ssl" else 587))
        new_port = int(new.get("port") or (465 if new_sec == "ssl" else 587))
        downgraded = old_sec in ("ssl", "starttls") and new_sec == "none"
        return same_host and old_port == new_port and not downgraded
    return True


def merge_secrets(kind: str, new: dict, old: dict) -> dict:
    """A secret sent back as *** means "unchanged" — only when the destination
    has not changed (prevents redirecting stored credentials to another host)."""
    out = dict(new)
    same_dest = _same_secret_dest(kind, out, old)
    for field in FIELDS[kind]["secret"]:
        if out.get(field) == MASK:
            if not old.get(field):
                out.pop(field, None)
            elif same_dest:
                out[field] = old[field]
            else:
                raise ValueError(f"The destination changed: enter the {field} again.")
    return out


def store_config(cfg: dict) -> str:
    return _encrypt_secret(json.dumps(cfg))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _text(title: str, body: str) -> str:
    return f"{title}\n{body}" if body else title


def _sanitize_header(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _mask_error(kind: str, cfg: dict, text: str) -> str:
    """Strip any channel secrets (e.g. Telegram bot_token, Slack/Discord webhook URL)
    out of error text before logging or saving into last_status."""
    secrets = sorted(
        {str(cfg[k]) for k in FIELDS.get(kind, {}).get("secret", ()) if cfg.get(k)},
        key=len,
        reverse=True,
    )
    for sec in secrets:
        text = text.replace(sec, MASK)
    return text


async def _send_one(kind: str, cfg: dict, payload: dict) -> None:
    title, body = payload["title"], payload["body"]
    timeout = aiohttp.ClientTimeout(total=15)
    if kind == "email":
        await asyncio.to_thread(_send_email, cfg, title, body)
        return
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(), **HTTP_CLIENT_LIMITS) as http:
        if kind == "webhook":
            raw = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "User-Agent": "Venvy"}
            if cfg.get("secret"):
                headers["X-Venvy-Signature"] = "sha256=" + hmac.new(cfg["secret"].encode(), raw, hashlib.sha256).hexdigest()
            resp = await http.post(cfg["url"], data=raw, headers=headers, allow_redirects=False)
        elif kind == "slack":
            resp = await http.post(cfg["url"], json={"text": f"*{title}*\n{body}" if body else f"*{title}*"}, allow_redirects=False)
        elif kind == "discord":
            resp = await http.post(cfg["url"], json={"content": f"**{title}**\n{body}"[:2000]}, allow_redirects=False)
        elif kind == "ntfy":
            base = (cfg.get("url") or "https://ntfy.sh").rstrip("/")
            headers = {"Title": _sanitize_header(title.encode("ascii", "ignore").decode()) or "Venvy"}
            headers["Priority"] = "high" if payload["level"] == "error" else "default"
            if cfg.get("token"):
                headers["Authorization"] = f"Bearer {cfg['token']}"
            topic = urllib.parse.quote(cfg["topic"], safe="")
            resp = await http.post(f"{base}/{topic}", data=body.encode() or b"-", headers=headers, allow_redirects=False)
        elif kind == "telegram":
            resp = await http.post(
                f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                json={"chat_id": cfg["chat_id"], "text": _text(title, body)[:4000]},
                allow_redirects=False)
        else:
            raise ValueError(f"unknown kind {kind}")
        async with resp:
            if resp.status >= 300:
                raise RuntimeError(f"HTTP {resp.status}")


def _send_email(cfg: dict, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = _sanitize_header(f"[Venvy] {title}")
    msg["From"] = _sanitize_header(cfg["from"])
    msg["To"] = _sanitize_header(cfg["to"])
    msg.set_content(body or title)
    host, port, security = cfg["host"], int(cfg.get("port") or 587), cfg.get("security", "starttls")
    ctx = ssl.create_default_context()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port, timeout=15)
    with smtp:
        if security == "starttls":
            smtp.starttls(context=ctx)
        if cfg.get("username"):
            smtp.login(cfg["username"], cfg.get("password", ""))
        smtp.send_message(msg)


def _matches(ch: NotifyChannel, event: str) -> bool:
    names = {e.strip() for e in (ch.events or "*").split(",") if e.strip()}
    return "*" in names or event in names


async def _deliver(event: str, payload: dict, only_channel: Optional[int] = None) -> List[dict]:
    def _load_targets():
        with Session(engine) as s:
            if only_channel is not None:
                ch = s.get(NotifyChannel, only_channel)
                channels = [ch] if ch else []
            else:
                channels = s.exec(select(NotifyChannel).where(NotifyChannel.enabled == True)).all()  # noqa: E712
            return [(c.id, c.kind, load_config(c)) for c in channels if only_channel is not None or _matches(c, event)]

    targets = await asyncio.to_thread(_load_targets)

    async def _one(cid: int, kind: str, cfg: dict) -> dict:
        try:
            await _send_one(kind, cfg, payload)
            status = "ok"
        except Exception as exc:
            raw_err = _mask_error(kind, cfg, str(exc) or exc.__class__.__name__)
            status = f"failed: {raw_err[:200]}"
            logging.warning(f"Notification channel {cid} ({kind}): {status}")

        def _save_status() -> None:
            with Session(engine) as s:
                row = s.get(NotifyChannel, cid)
                if row:
                    row.last_status, row.last_sent_at = status, utc_now()
                    s.add(row)
                    s.commit()

        await asyncio.to_thread(_save_status)
        return {"channel": cid, "status": status}

    if not targets:
        return []
    return list(await asyncio.gather(*(_one(cid, kind, cfg) for cid, kind, cfg in targets)))


def _payload(event: str, title: str, body: str, app_id, app_name, level: str) -> dict:
    return {"event": event, "title": title, "body": body, "level": level,
            "app_id": app_id, "app": app_name, "time": utc_now().isoformat()}


def emit(event: str, title: str, body: str = "", app_id: Optional[int] = None,
         app_name: Optional[str] = None, level: str = "info") -> None:
    """Send an event to the channels that listen for it. Fire and forget."""
    now = time.monotonic()
    key = (event, app_id, title)
    with _last_sent_lock:
        prev = _last_sent.get(key)
        if prev is not None and now - prev < _COOLDOWN_SEC:
            return
        _last_sent[key] = now
        if len(_last_sent) > 2000:
            for k in [k for k, t in _last_sent.items() if now - t > 600]:
                _last_sent.pop(k, None)
    payload = _payload(event, title, body, app_id, app_name, level)

    def _start() -> None:
        t = asyncio.ensure_future(_deliver(event, payload))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)

    try:
        asyncio.get_running_loop()
        _start()
    except RuntimeError:
        if _loop and not _loop.is_closed():
            _loop.call_soon_threadsafe(_start)


async def send_test(channel_id: int) -> dict:
    payload = _payload("test", "Venvy test notification", "If you can read this, the channel works.", None, None, "info")
    results = await _deliver("test", payload, only_channel=channel_id)
    return results[0] if results else {"channel": channel_id, "status": "failed: no such channel"}
