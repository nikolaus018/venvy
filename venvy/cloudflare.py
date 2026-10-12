"""Cloudflare API helpers: stored credentials, requests, zones and DNS records."""
import asyncio
import logging
import re
import unicodedata
import urllib.parse
from typing import Callable, List, Optional

import aiohttp
from fastapi import HTTPException
from sqlmodel import Session

from venvy.crypto import _decrypt_secret
from venvy.models import SystemSetting


def _sanitize_cf_value(v: str) -> str:
    """Strip ALL whitespace and invisible/zero-width Unicode chars."""
    v = v.strip()
    v = "".join(ch for ch in v if ch not in "\r\n\t")
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")
    return v


# ---------------------------------------------------------------------------
# Cloudflare Tunnel Route Management
# ---------------------------------------------------------------------------

def _get_cf_creds(session: Session) -> dict:
    """Return sanitized dict with api_token, account_id, tunnel_id or raise 400."""
    keys = ("cf_api_token", "cf_account_id", "cf_tunnel_id")
    result = {}
    for k in keys:
        row = session.get(SystemSetting, k)
        raw = row.value if row else ""
        decrypted = _decrypt_secret(raw) if raw else ""
        result[k] = _sanitize_cf_value(decrypted)
    missing = [k for k, v in result.items() if not v]
    if missing:
        raise HTTPException(400, f"Cloudflare not configured. Missing: {', '.join(missing)}")
    # Both ids are pasted in by hand and end up inside API URLs.
    for k in ("cf_account_id", "cf_tunnel_id"):
        if not _CF_ID_RE.match(result[k]):
            label = "Account ID" if k == "cf_account_id" else "Tunnel ID"
            raise HTTPException(400, f"The saved Cloudflare {label} is not a valid id — re-enter it under System → Cloudflare.")
    return result


_CF_ID_RE = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")


# ---------------------------------------------------------------------------
# Cloudflare API helper
# ---------------------------------------------------------------------------

async def _cf_request(method: str, url: str, token: str, payload: Optional[dict] = None) -> dict:
    """Make an asynchronous Cloudflare API call with proper timeout and error handling."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, json=payload) as resp:
                data = await resp.json()
                if not isinstance(data, dict):
                    raise HTTPException(502, f"Cloudflare API returned an unexpected response (HTTP {resp.status}).")
                # Cloudflare also reports failures as 200 with success=false.
                if not (200 <= resp.status < 300) or data.get("success") is False:
                    errs = data.get("errors") or []
                    msg = errs[0].get("message") if errs and isinstance(errs[0], dict) else f"HTTP {resp.status}"
                    status = resp.status if resp.status >= 400 else 502
                    raise HTTPException(status, f"Cloudflare API Error: {msg}")
                return data
    except asyncio.TimeoutError:
        raise HTTPException(504, "Cloudflare API request timed out (10s).")
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Cloudflare connection error: {str(exc)}")
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(500, f"Internal error during Cloudflare request: {str(exc)}")


# The tunnel's ingress list is one document: every change is "read it all,
# write it all back". Two of those interleaved (two apps published at the
# same moment) and the second write silently dropped the first one's route.
_ingress_lock = asyncio.Lock()


_CATCH_ALL = [{"service": "http_status:404"}]


async def _cf_update_ingress(creds: dict, change: Callable[[List[dict]], List[dict]]) -> List[dict]:
    """Rewrite the tunnel's hostname rules: *change* receives the current
    named rules and returns the new ones; the catch-all rule is kept (or
    added) at the end, where Cloudflare requires it. Returns the complete
    ingress list as it was before, for callers that may need to put it back
    with _cf_put_ingress()."""
    cfg_url = _cf_tunnel_cfg_url(creds)
    async with _ingress_lock:
        current = await _cf_request("GET", cfg_url, creds["cf_api_token"])
        ingress = ((current.get("result") or {}).get("config") or {}).get("ingress") or []
        named = [r for r in ingress if r.get("hostname")]
        catch_all = [r for r in ingress if not r.get("hostname")]
        new_ingress = change(named) + (catch_all or _CATCH_ALL)
        await _cf_request("PUT", cfg_url, creds["cf_api_token"], {"config": {"ingress": new_ingress}})
    return ingress


async def _cf_put_ingress(creds: dict, ingress: List[dict]) -> None:
    """Write a complete ingress list as it is (used to undo an update)."""
    async with _ingress_lock:
        await _cf_request("PUT", _cf_tunnel_cfg_url(creds), creds["cf_api_token"],
                          {"config": {"ingress": ingress or _CATCH_ALL}})


def _cf_tunnel_cfg_url(creds: dict) -> str:
    return (f"https://api.cloudflare.com/client/v4/accounts/{creds['cf_account_id']}"
            f"/cfd_tunnel/{creds['cf_tunnel_id']}/configurations")


_CF_HOSTNAME_RE = re.compile(
    r"\A(?:\*\.)?(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _clean_cf_hostname(raw: str) -> str:
    """Normalise a user-typed hostname and make sure it is only a hostname.
    It ends up inside Cloudflare API URLs and tunnel/DNS records."""
    host = raw.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if len(host) > 253 or not _CF_HOSTNAME_RE.match(host):
        raise HTTPException(400, "Invalid hostname — use a plain name like app.example.com.")
    return host


async def _cf_find_zone_id(hostname: str, account_id: str, api_token: str) -> Optional[str]:
    """Resolve the Cloudflare Zone ID for the domain that owns *hostname*."""
    parts = hostname.split(".")
    candidates = [".".join(parts[i:]) for i in range(1, len(parts) - 1)]
    if hostname not in candidates:
        candidates.insert(0, hostname)
    candidates = list(dict.fromkeys(candidates))
    for candidate in candidates:
        url = (
            f"https://api.cloudflare.com/client/v4/zones"
            f"?name={urllib.parse.quote(candidate, safe='')}&account.id={urllib.parse.quote(account_id, safe='')}&status=active"
        )
        try:
            data = await _cf_request("GET", url, api_token)
            zones = data.get("result") or []
            if zones:
                logging.info(f"CF zone found: {candidate!r} → {zones[0]['id']}")
                return zones[0]["id"]
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise HTTPException(
                    403,
                    "Cloudflare API token lacks Zone:Read permission. "
                    "Add Zone:Read and DNS:Edit to your token, then retry."
                )
            raise
        except Exception:
            continue
    return None


async def _cf_upsert_dns_cname(
    zone_id: str,
    hostname: str,
    tunnel_id: str,
    api_token: str,
) -> str:
    """Create or update a CNAME DNS record in Cloudflare pointing to the tunnel."""
    target = f"{tunnel_id}.cfargotunnel.com"

    record_body = {
        "type":    "CNAME",
        "name":    hostname,
        "content": target,
        "ttl":     1,
        "proxied": True,
    }

    list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=CNAME&name={urllib.parse.quote(hostname, safe='')}&per_page=100"
    existing = await _cf_request("GET", list_url, api_token)
    cname_records = existing.get("result") or []

    for rec_type in ("A", "AAAA"):
        conflict_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type={rec_type}&name={urllib.parse.quote(hostname, safe='')}&per_page=100"
        try:
            conflict_data = await _cf_request("GET", conflict_url, api_token)
            for rec in conflict_data.get("result") or []:
                logging.warning(f"Deleting conflicting {rec_type} record {rec['id']} for {hostname}")
                await _cf_request(
                    "DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                    api_token,
                )
        except Exception as exc:
            logging.warning(f"Could not clear {rec_type} records for {hostname}: {exc}")

    if cname_records:
        for extra in cname_records[1:]:
            try:
                await _cf_request(
                    "DELETE",
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{extra['id']}",
                    api_token,
                )
            except Exception as exc:
                logging.warning(f"Could not delete duplicate CNAME {extra['id']}: {exc}")
        record_id = cname_records[0]["id"]
        resp = await _cf_request(
            "PUT",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
            api_token,
            record_body,
        )
    else:
        resp = await _cf_request(
            "POST",
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            api_token,
            record_body,
        )

    if not resp.get("success"):
        errs = resp.get("errors") or []
        err_msgs = "; ".join(e.get("message", str(e)) for e in errs)
        raise HTTPException(502, f"DNS CNAME upsert failed: {err_msgs or errs}")

    result = resp.get("result") or {}
    record_id = result.get("id", "unknown")
    logging.info(
        f"DNS CNAME upserted: {hostname} → {target} "
        f"(record {record_id}, proxied={result.get('proxied')})"
    )
    return record_id


async def _cf_delete_dns_cname(
    zone_id: str,
    hostname: str,
    api_token: str,
) -> None:
    """Delete the CNAME DNS record for *hostname* in the given zone, if it exists."""
    list_url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records?type=CNAME&name={urllib.parse.quote(hostname, safe='')}"
    try:
        existing = await _cf_request("GET", list_url, api_token)
        for rec in existing.get("result", []):
            await _cf_request(
                "DELETE",
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec['id']}",
                api_token,
            )
    except HTTPException:
        raise
    except Exception as exc:
        logging.warning(f"DNS record deletion failed for {hostname}: {exc}")
