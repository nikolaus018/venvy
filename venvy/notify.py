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
import smtplib
import ssl
import time
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
    "webhook": {"required": ("url",), "secret": ("secret",)},
    "slack": {"required": ("url",), "secret": ("url",)},
    "discord": {"required": ("url",), "secret": ("url",)},
    "ntfy": {"required": ("topic",), "secret": ("token",)},
    "telegram": {"required": ("bot_token", "chat_id"), "secret": ("bot_token",)},
    "email": {"required": ("host", "from", "to"), "secret": ("password",)},
}
MASK = "***"

_loop: Optional[asyncio.AbstractEventLoop] = None
_last_sent: Dict[tuple, float] = {}
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


def check_config(kind: str, cfg: dict) -> dict:
    """Clean and validate a channel's settings; raises ValueError with a message."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    out = {k: (str(v).strip() if isinstance(v, str) else v) for k, v in cfg.items() if v is not None}
    for field in FIELDS[kind]["required"]:
        if not out.get(field):
            raise ValueError(f"{field} is required for a {kind} channel.")
    for field in ("url",):
        if out.get(field) and not str(out[field]).startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
    if kind == "email":
        try:
            out["port"] = int(out.get("port") or (465 if out.get("security") == "ssl" else 587))
        except (TypeError, ValueError):
            raise ValueError("port must be a number.")
        if out.get("security", "starttls") not in ("starttls", "ssl", "none"):
            raise ValueError("security must be starttls, ssl or none.")
        out.setdefault("security", "starttls")
    return out


def masked(kind: str, cfg: dict) -> dict:
    secret = FIELDS[kind]["secret"]
    return {k: (MASK if k in secret and v else v) for k, v in cfg.items()}


def merge_secrets(kind: str, new: dict, old: dict) -> dict:
    """A secret sent back as *** means "unchanged"."""
    out = dict(new)
    for field in FIELDS[kind]["secret"]:
        if out.get(field) == MASK:
            if old.get(field):
                out[field] = old[field]
            else:
                out.pop(field, None)
    return out


def store_config(cfg: dict) -> str:
    return _encrypt_secret(json.dumps(cfg))


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _text(title: str, body: str) -> str:
    return f"{title}\n{body}" if body else title


async def _send_one(kind: str, cfg: dict, payload: dict) -> None:
    title, body = payload["title"], payload["body"]
    timeout = aiohttp.ClientTimeout(total=15)
    if kind == "email":
        await asyncio.to_thread(_send_email, cfg, title, body)
        return
    async with aiohttp.ClientSession(timeout=timeout, **HTTP_CLIENT_LIMITS) as http:
        if kind == "webhook":
            raw = json.dumps(payload).encode()
            headers = {"Content-Type": "application/json", "User-Agent": "Venvy"}
            if cfg.get("secret"):
                headers["X-Venvy-Signature"] = "sha256=" + hmac.new(cfg["secret"].encode(), raw, hashlib.sha256).hexdigest()
            resp = await http.post(cfg["url"], data=raw, headers=headers)
        elif kind == "slack":
            resp = await http.post(cfg["url"], json={"text": f"*{title}*\n{body}" if body else f"*{title}*"})
        elif kind == "discord":
            resp = await http.post(cfg["url"], json={"content": f"**{title}**\n{body}"[:2000]})
        elif kind == "ntfy":
            base = (cfg.get("url") or "https://ntfy.sh").rstrip("/")
            headers = {"Title": title.encode("ascii", "ignore").decode() or "Venvy"}
            headers["Priority"] = "high" if payload["level"] == "error" else "default"
            if cfg.get("token"):
                headers["Authorization"] = f"Bearer {cfg['token']}"
            resp = await http.post(f"{base}/{cfg['topic']}", data=body.encode() or b"-", headers=headers)
        elif kind == "telegram":
            resp = await http.post(
                f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                json={"chat_id": cfg["chat_id"], "text": _text(title, body)[:4000]})
        else:
            raise ValueError(f"unknown kind {kind}")
        async with resp:
            if resp.status >= 300:
                raise RuntimeError(f"HTTP {resp.status}: {(await resp.text())[:200]}")


def _send_email(cfg: dict, title: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = f"[Venvy] {title}"
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
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
    with Session(engine) as s:
        channels = s.exec(select(NotifyChannel).where(NotifyChannel.enabled == True)).all()  # noqa: E712
        if only_channel is not None:
            channels = [c for c in s.exec(select(NotifyChannel)).all() if c.id == only_channel]
        targets = [(c.id, c.kind, load_config(c)) for c in channels if only_channel is not None or _matches(c, event)]
    results = []
    for cid, kind, cfg in targets:
        try:
            await _send_one(kind, cfg, payload)
            status = "ok"
        except Exception as exc:
            status = f"failed: {str(exc)[:200] or exc.__class__.__name__}"
            logging.warning(f"Notification channel {cid} ({kind}): {status}")
        results.append({"channel": cid, "status": status})
        with Session(engine) as s:
            row = s.get(NotifyChannel, cid)
            if row:
                row.last_status, row.last_sent_at = status, utc_now()
                s.add(row)
                s.commit()
    return results


def _payload(event: str, title: str, body: str, app_id, app_name, level: str) -> dict:
    return {"event": event, "title": title, "body": body, "level": level,
            "app_id": app_id, "app": app_name, "time": utc_now().isoformat()}


def emit(event: str, title: str, body: str = "", app_id: Optional[int] = None,
         app_name: Optional[str] = None, level: str = "info") -> None:
    """Send an event to the channels that listen for it. Fire and forget."""
    now = time.monotonic()
    key = (event, app_id, title)
    if now - _last_sent.get(key, 0.0) < _COOLDOWN_SEC:
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
