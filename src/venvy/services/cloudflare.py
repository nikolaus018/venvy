"""Cloudflare Tunnel and DNS integration.

Publishing an app means two things happen together:

1. an ingress rule is added to the tunnel's configuration, mapping the public
   hostname to ``http://<lan-ip>:<port>``, and
2. a proxied CNAME is created in the zone, pointing the hostname at
   ``<tunnel-id>.cfargotunnel.com``.

Both must succeed. Because Cloudflare has no transaction across the two, the
publish flow rolls the ingress change back by hand if the DNS half fails —
otherwise the tunnel would carry a rule for a hostname that resolves nowhere.

Credentials live in :class:`~venvy.db.models.SystemSetting` rows prefixed
``cf_`` and are encrypted at rest.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata

import aiohttp
from fastapi import HTTPException, status
from sqlmodel import Session

from venvy.core.crypto import decrypt_secret
from venvy.db.models import SystemSetting

__all__ = [
    "CREDENTIAL_KEYS",
    "CloudflareCredentials",
    "build_ingress",
    "cf_request",
    "delete_dns_cname",
    "find_zone_id",
    "get_credentials",
    "get_tunnel_config",
    "list_zones",
    "normalize_hostname",
    "put_tunnel_ingress",
    "sanitize_credential",
    "upsert_dns_cname",
    "verify_credentials",
]

logger = logging.getLogger(__name__)

API_BASE = "https://api.cloudflare.com/client/v4"

#: Settings keys holding the integration's credentials.
CREDENTIAL_KEYS = ("cf_api_token", "cf_account_id", "cf_tunnel_id")

_REQUEST_TIMEOUT_SECONDS = 10
_ZONE_LIST_TIMEOUT_SECONDS = 12
_ZONE_PAGE_SIZE = 50
#: Bound on zone pagination, so a huge account cannot stall a request forever.
_ZONE_MAX_PAGES = 10

#: Rule Cloudflare requires at the end of every ingress list.
_CATCH_ALL = {"service": "http_status:404"}


class CloudflareCredentials(dict):
    """The three values every Cloudflare call needs.

    A ``dict`` subclass so existing call sites can keep subscripting it, with
    named properties for readability.
    """

    @property
    def api_token(self) -> str:
        return self["cf_api_token"]

    @property
    def account_id(self) -> str:
        return self["cf_account_id"]

    @property
    def tunnel_id(self) -> str:
        return self["cf_tunnel_id"]


def sanitize_credential(value: str) -> str:
    """Strip whitespace and invisible Unicode from a pasted credential.

    Copying a token out of the Cloudflare dashboard routinely brings along a
    zero-width character or a stray newline, which produces a baffling 403 from
    the API. Removing every ``Cf`` (format) character makes paste just work.
    """
    value = value.strip()
    value = "".join(ch for ch in value if ch not in "\r\n\t")
    return "".join(ch for ch in value if unicodedata.category(ch) != "Cf")


def normalize_hostname(hostname: str) -> str:
    """Lower-case a hostname and strip any scheme or trailing slash."""
    cleaned = hostname.strip().lower()
    for prefix in ("https://", "http://"):
        cleaned = cleaned.removeprefix(prefix)
    return cleaned.rstrip("/")


def get_credentials(session: Session) -> CloudflareCredentials:
    """Load and decrypt the Cloudflare credentials.

    :raises HTTPException: 400 listing which keys are missing.
    """
    resolved = CloudflareCredentials()
    for key in CREDENTIAL_KEYS:
        row = session.get(SystemSetting, key)
        raw = row.value if row else ""
        resolved[key] = sanitize_credential(decrypt_secret(raw) if raw else "")

    missing = [key for key, value in resolved.items() if not value]
    if missing:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Cloudflare is not configured. Missing: {', '.join(missing)}",
        )
    return resolved


async def cf_request(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    """Call the Cloudflare API, translating failures into HTTP errors.

    :raises HTTPException: with Cloudflare's own message where one is available,
        502 for connection problems, 504 on timeout.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)

    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as http,
            http.request(method, url, headers=headers, json=payload) as response,
        ):
            data = await response.json()
            if not 200 <= response.status < 300:
                errors = data.get("errors") or []
                message = errors[0].get("message") if errors else f"HTTP {response.status}"
                raise HTTPException(response.status, f"Cloudflare API error: {message}")
            return data
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT,
            f"Cloudflare API request timed out after {_REQUEST_TIMEOUT_SECONDS}s.",
        ) from exc
    except aiohttp.ClientError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Cloudflare connection error: {exc}"
        ) from exc


def _zone_permission_error() -> HTTPException:
    """The 403 shown when the API token cannot read zones."""
    return HTTPException(
        status.HTTP_403_FORBIDDEN,
        "The Cloudflare API token lacks Zone:Read permission, so Venvy cannot manage DNS. "
        "Add Zone:Read and DNS:Edit to the token, then retry.",
    )


async def find_zone_id(hostname: str, account_id: str, api_token: str) -> str | None:
    """Resolve the zone that owns *hostname*, or None if the account has none.

    Tries progressively shorter suffixes — ``a.b.example.com`` then
    ``b.example.com`` then ``example.com`` — because the zone is registered
    against the registrable domain, not the full hostname.
    """
    parts = hostname.split(".")
    candidates = [".".join(parts[index:]) for index in range(1, len(parts) - 1)]
    candidates.insert(0, hostname)
    for candidate in dict.fromkeys(candidates):
        url = f"{API_BASE}/zones?name={candidate}&account.id={account_id}&status=active"
        try:
            data = await cf_request("GET", url, api_token)
        except HTTPException as exc:
            if exc.status_code in (401, 403):
                raise _zone_permission_error() from exc
            raise
        zones = data.get("result") or []
        if zones:
            logger.info("Cloudflare zone for %r resolved to %s", candidate, zones[0]["id"])
            return zones[0]["id"]
    return None


async def _delete_records(zone_id: str, record_ids: list[str], api_token: str) -> None:
    """Delete DNS records by id, logging but tolerating individual failures."""
    for record_id in record_ids:
        try:
            await cf_request(
                "DELETE", f"{API_BASE}/zones/{zone_id}/dns_records/{record_id}", api_token
            )
        except HTTPException as exc:
            logger.warning("Could not delete DNS record %s: %s", record_id, exc.detail)


async def upsert_dns_cname(zone_id: str, hostname: str, tunnel_id: str, api_token: str) -> str:
    """Point *hostname* at the tunnel with a proxied CNAME. Returns the record id.

    Any A/AAAA records for the same name are removed first: they would take
    precedence over the CNAME and silently send traffic to the old address.
    Duplicate CNAMEs are collapsed to one.
    """
    body = {
        "type": "CNAME",
        "name": hostname,
        "content": f"{tunnel_id}.cfargotunnel.com",
        "ttl": 1,  # 1 means "automatic" to Cloudflare
        "proxied": True,
    }

    existing = await cf_request(
        "GET",
        f"{API_BASE}/zones/{zone_id}/dns_records?type=CNAME&name={hostname}&per_page=100",
        api_token,
    )
    cnames = existing.get("result") or []

    for record_type in ("A", "AAAA"):
        try:
            conflicts = await cf_request(
                "GET",
                f"{API_BASE}/zones/{zone_id}/dns_records?type={record_type}&name={hostname}&per_page=100",
                api_token,
            )
        except HTTPException as exc:
            logger.warning(
                "Could not check %s records for %s: %s", record_type, hostname, exc.detail
            )
            continue
        conflicting_ids = [record["id"] for record in conflicts.get("result") or []]
        if conflicting_ids:
            logger.warning(
                "Removing %d conflicting %s record(s) for %s",
                len(conflicting_ids),
                record_type,
                hostname,
            )
            await _delete_records(zone_id, conflicting_ids, api_token)

    if cnames:
        await _delete_records(zone_id, [record["id"] for record in cnames[1:]], api_token)
        response = await cf_request(
            "PUT", f"{API_BASE}/zones/{zone_id}/dns_records/{cnames[0]['id']}", api_token, body
        )
    else:
        response = await cf_request(
            "POST", f"{API_BASE}/zones/{zone_id}/dns_records", api_token, body
        )

    if not response.get("success"):
        errors = response.get("errors") or []
        detail = "; ".join(error.get("message", str(error)) for error in errors)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"DNS CNAME upsert failed: {detail or errors}"
        )

    record = response.get("result") or {}
    record_id = record.get("id", "unknown")
    logger.info(
        "DNS CNAME set: %s → %s.cfargotunnel.com (record %s, proxied=%s)",
        hostname,
        tunnel_id,
        record_id,
        record.get("proxied"),
    )
    return record_id


async def delete_dns_cname(zone_id: str, hostname: str, api_token: str) -> None:
    """Remove the CNAME for *hostname*, if one exists."""
    try:
        existing = await cf_request(
            "GET",
            f"{API_BASE}/zones/{zone_id}/dns_records?type=CNAME&name={hostname}",
            api_token,
        )
    except HTTPException as exc:
        logger.warning("Could not list DNS records for %s: %s", hostname, exc.detail)
        return
    await _delete_records(
        zone_id, [record["id"] for record in existing.get("result", [])], api_token
    )


def _tunnel_config_url(account_id: str, tunnel_id: str) -> str:
    """URL of the tunnel's ingress configuration."""
    return f"{API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"


async def get_tunnel_config(account_id: str, tunnel_id: str, api_token: str) -> list[dict]:
    """Return the tunnel's current ingress rules."""
    data = await cf_request("GET", _tunnel_config_url(account_id, tunnel_id), api_token)
    if not data.get("success"):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Cloudflare could not return the tunnel configuration: {data.get('errors')}",
        )
    return (data.get("result") or {}).get("config", {}).get("ingress") or []


async def put_tunnel_ingress(
    account_id: str, tunnel_id: str, api_token: str, ingress: list[dict]
) -> None:
    """Replace the tunnel's ingress rules with *ingress*."""
    response = await cf_request(
        "PUT",
        _tunnel_config_url(account_id, tunnel_id),
        api_token,
        {"config": {"ingress": ingress}},
    )
    if not response.get("success"):
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Cloudflare tunnel update failed: {response.get('errors')}",
        )


def build_ingress(
    existing: list[dict], drop_hostnames: set[str], add: dict | None = None
) -> list[dict]:
    """Rebuild an ingress list without *drop_hostnames* and with *add* appended.

    The catch-all rule is preserved and always ends up last, which Cloudflare
    requires; if the tunnel had none, a 404 catch-all is supplied.
    """
    catch_all = [rule for rule in existing if not rule.get("hostname")]
    named = [
        rule for rule in existing if rule.get("hostname") and rule["hostname"] not in drop_hostnames
    ]
    if add:
        named.append(add)
    return named + (catch_all or [_CATCH_ALL])


async def list_zones(api_token: str) -> list[dict[str, str]]:
    """Return every active zone on the account as ``{"name", "id"}``.

    Partial results are returned rather than raising if pagination stalls — a
    dropdown with most of the domains beats an error page.
    """
    zones: list[dict[str, str]] = []

    async def fetch_all() -> None:
        page = 1
        while page <= _ZONE_MAX_PAGES:
            url = f"{API_BASE}/zones?per_page={_ZONE_PAGE_SIZE}&page={page}&status=active"
            try:
                data = await cf_request("GET", url, api_token)
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    raise _zone_permission_error() from exc
                raise
            for zone in data.get("result") or []:
                name, zone_id = zone.get("name", ""), zone.get("id", "")
                if name and zone_id:
                    zones.append({"name": name, "id": zone_id})
            total_pages = (data.get("result_info") or {}).get("total_pages", 1)
            if page >= total_pages:
                break
            page += 1

    try:
        await asyncio.wait_for(fetch_all(), timeout=_ZONE_LIST_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("Cloudflare zone listing timed out — returning partial results.")
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Cloudflare zone listing failed: %s", exc)
        if not zones:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, f"Cloudflare API error: {exc}"
            ) from exc

    zones.sort(key=lambda zone: zone["name"])
    return zones


async def verify_credentials(credentials: CloudflareCredentials) -> dict:
    """Check the account, tunnel and zone permissions the integration needs.

    Returns a per-check report rather than raising, so the settings UI can show
    exactly which of the three is misconfigured.
    """
    token = credentials.api_token
    account_id = credentials.account_id
    checks: dict[str, dict] = {}

    checks["account"] = await _check_account(account_id, token)
    checks["tunnel"] = await _check_tunnel(account_id, credentials.tunnel_id, token)
    checks["dns_zones"] = await _check_zones(account_id, token)

    return {"ok": all(check["ok"] for check in checks.values()), "checks": checks}


async def _check_account(account_id: str, token: str) -> dict:
    """Confirm the token can read the account."""
    try:
        data = await cf_request("GET", f"{API_BASE}/accounts/{account_id}", token)
    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}
    if not data.get("success"):
        return {"ok": False, "error": _error_text(data)}
    return {"ok": True, "name": (data.get("result") or {}).get("name", account_id)}


async def _check_tunnel(account_id: str, tunnel_id: str, token: str) -> dict:
    """Confirm the tunnel exists and report whether a connector is attached."""
    try:
        data = await cf_request(
            "GET", f"{API_BASE}/accounts/{account_id}/cfd_tunnel/{tunnel_id}", token
        )
    except HTTPException as exc:
        return {"ok": False, "error": str(exc.detail)}
    if not data.get("success"):
        return {"ok": False, "error": _error_text(data)}

    result = data.get("result") or {}
    name = result.get("name", tunnel_id)
    tunnel_status = result.get("status", "unknown")
    connections = result.get("connections") or []
    active = len([c for c in connections if c.get("is_pending_reconnect") is False])

    report = {
        "ok": True,
        "name": name,
        "id": result.get("id", ""),
        "status": tunnel_status,
        "active_connections": active,
        "message": f"{name} ({tunnel_status}) — {active} active connection(s)",
    }
    if active == 0:
        # The single most common support question: published routes return
        # Cloudflare error 1016 when nothing is on the other end of the tunnel.
        report["warning"] = (
            "No active connections — the cloudflared connector does not appear to be "
            "running. Start cloudflared on this server or published routes will return "
            "Error 1016."
        )
    return report


async def _check_zones(account_id: str, token: str) -> dict:
    """Confirm the token can list zones, which DNS management requires."""
    try:
        data = await cf_request(
            "GET", f"{API_BASE}/zones?account.id={account_id}&per_page=5", token
        )
    except HTTPException as exc:
        return {
            "ok": False,
            "error": f"{exc.detail} — add Zone:Read and DNS:Edit to the token.",
        }
    if not data.get("success"):
        return {
            "ok": False,
            "error": "Cannot list zones — add Zone:Read and DNS:Edit permissions to the "
            "API token for automatic DNS management.",
        }
    count = len(data.get("result", []))
    return {
        "ok": True,
        "message": f"Found {count} zone(s) — DNS CNAME records will be managed automatically.",
    }


def _error_text(data: dict) -> str:
    """Join Cloudflare's error messages into one line."""
    messages = "; ".join(error.get("message", "") for error in data.get("errors", []))
    return messages or "Unknown error"
