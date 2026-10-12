"""Cloudflare routes: credential checks, zones, and per-app tunnel routes."""
import asyncio
import json
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select, Session

from venvy.audit import audit_log
from venvy.auth import (
    check_admin,
    check_ip_allowlist,
    get_current_user,
    rate_limit_mutations,
    require_operator,
)
from venvy.cloudflare import (
    _cf_delete_dns_cname,
    _cf_find_zone_id,
    _cf_put_ingress,
    _cf_request,
    _cf_tunnel_cfg_url,
    _cf_update_ingress,
    _cf_upsert_dns_cname,
    _clean_cf_hostname,
    _get_cf_creds,
    _sanitize_cf_value,
)
from venvy.crypto import _decrypt_secret
from venvy.db import get_session
from venvy.models import App, CfRoute, SystemSetting, User
from venvy.process import get_server_ipv4

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist)]
)


@router.post("/system/cloudflare/test")
async def test_cf_credentials(
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Verify saved Cloudflare integration credentials and permissions."""
    import urllib.request as _ur
    import urllib.error  as _ue

    def _fetch(url: str, token: str):
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        req  = _ur.Request(url, headers=hdrs)
        with _ur.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    creds = _get_cf_creds(session)
    token      = creds["cf_api_token"]
    account_id = creds["cf_account_id"]
    tunnel_id  = creds["cf_tunnel_id"]

    results = {}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}",
            token,
        )
        if data.get("success"):
            results["account"] = {"ok": True, "name": data["result"].get("name", account_id)}
        else:
            msgs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            results["account"] = {"ok": False, "error": msgs or "Unknown error"}
    except _ue.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            msgs = "; ".join(e.get("message", "") for e in json.loads(raw).get("errors", []))
        except Exception:
            msgs = raw[:200]
        results["account"] = {"ok": False, "error": f"HTTP {exc.code}: {msgs}"}
    except Exception as exc:
        results["account"] = {"ok": False, "error": str(exc)}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            token,
        )
        if data.get("success"):
            result      = data.get("result") or {}
            actual_id   = result.get("id", "")
            tunnel_name = result.get("name", tunnel_id)
            status      = result.get("status", "unknown")
            connections  = result.get("connections") or []
            active_conns = len([c for c in connections if c.get("is_pending_reconnect") is False])
            results["tunnel"] = {
                "ok":               True,
                "name":             tunnel_name,
                "id":               actual_id,
                "status":           status,
                "active_connections": active_conns,
                "message":          f"{tunnel_name} ({status}) — {active_conns} active connection(s)",
            }
            if active_conns == 0:
                results["tunnel"]["warning"] = (
                    "No active connections — the cloudflared connector may not be running. "
                    "Start cloudflared on your server or the tunnel will give Error 1016."
                )
        else:
            msgs = "; ".join(e.get("message", "") for e in data.get("errors", []))
            results["tunnel"] = {"ok": False, "error": msgs or "Unknown error"}
    except _ue.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            msgs = "; ".join(e.get("message", "") for e in json.loads(raw).get("errors", []))
        except Exception:
            msgs = raw[:200]
        results["tunnel"] = {"ok": False, "error": f"HTTP {exc.code}: {msgs}"}
    except Exception as exc:
        results["tunnel"] = {"ok": False, "error": str(exc)}

    try:
        data = await asyncio.to_thread(
            _fetch,
            f"https://api.cloudflare.com/client/v4/zones?account.id={account_id}&per_page=5",
            token,
        )
        if data.get("success"):
            zone_count = len(data.get("result", []))
            results["dns_zones"] = {
                "ok": True,
                "message": f"Found {zone_count} zone(s) — DNS CNAME records will be managed automatically.",
            }
        else:
            results["dns_zones"] = {
                "ok": False,
                "error": "Cannot list zones — add Zone:Read and DNS:Edit permissions to your API token for automatic DNS management.",
            }
    except _ue.HTTPError as exc:
        results["dns_zones"] = {
            "ok": False,
            "error": f"HTTP {exc.code}: Zone access denied. Add Zone:Read and DNS:Edit to your token.",
        }
    except Exception as exc:
        results["dns_zones"] = {"ok": False, "error": str(exc)}

    all_ok = all(v["ok"] for v in results.values())
    return {"ok": all_ok, "checks": results}


@router.get("/system/cloudflare/status")
def cf_status(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """Whether the Cloudflare integration is configured — what the tunnel UI
    needs to know, for everyone allowed to publish routes, without handing
    out the API token."""
    def _value(key: str) -> str:
        row = session.get(SystemSetting, key)
        return _sanitize_cf_value(_decrypt_secret(row.value)) if row and row.value else ""

    token, account, tunnel = _value("cf_api_token"), _value("cf_account_id"), _value("cf_tunnel_id")
    return {
        "configured": bool(token and account and tunnel),
        "token_set": bool(token),
        "account_id_hint": account[:8],
        "tunnel_id_hint": tunnel[:8],
    }


@router.get("/system/cloudflare/zones")
async def list_cf_zones(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """Return all active zones (domains) on the Cloudflare account."""
    creds      = _get_cf_creds(session)
    api_token  = creds["cf_api_token"]

    zones: List[Dict[str, str]] = []
    page        = 1
    per_page    = 50

    async def _fetch_zones():
        nonlocal page
        while True:
            url = (
                f"https://api.cloudflare.com/client/v4/zones"
                f"?per_page={per_page}&page={page}&status=active"
            )
            try:
                data = await _cf_request("GET", url, api_token)
            except HTTPException as exc:
                if exc.status_code in (401, 403):
                    raise HTTPException(
                        403,
                        "Cloudflare API token lacks Zone:Read permission — cannot list domains. "
                        "Add Zone:Read and DNS:Edit to your token."
                    )
                raise

            result_list = data.get("result") or []
            for z in result_list:
                name = z.get("name", "")
                zid  = z.get("id",   "")
                if name and zid:
                    zones.append({"name": name, "id": zid})

            result_info = data.get("result_info") or {}
            total_pages = result_info.get("total_pages", 1)
            if page >= total_pages or page >= 10:
                break
            page += 1

    try:
        await asyncio.wait_for(_fetch_zones(), timeout=12.0)
    except asyncio.TimeoutError:
        logging.warning("Cloudflare zone list timed out — returning partial results.")
    except HTTPException:
        raise
    except Exception as exc:
        logging.error(f"CF zone list error: {exc}")
        if not zones:
            raise HTTPException(502, f"Cloudflare API error: {exc}")

    zones.sort(key=lambda z: z["name"])
    return {"ok": True, "zones": zones}


@router.get("/apps/{app_id}/tunnel")
def list_cf_routes(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    routes = session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all()
    return {
        "routes": [
            {
                "hostname": route.hostname,
                "local_port": route.local_port,
                "published": bool(route.cf_route_id),
                "cf_route_id": route.cf_route_id,
            }
            for route in routes
        ]
    }


class CfPublishReq(BaseModel):
    hostname: str
    local_port: int


@router.get("/apps/{app_id}/tunnel/check")
async def check_cf_hostname(
    app_id: int,
    hostname: str = Query(...),
    local_port: Optional[int] = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
):
    """Pre-flight check before publishing a hostname."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    clean = _clean_cf_hostname(hostname)

    existing_route = session.exec(
        select(CfRoute).where(CfRoute.hostname == clean)
    ).first()
    if existing_route and existing_route.app_id != app_id and existing_route.cf_route_id:
        other_app = session.get(App, existing_route.app_id)
        other_name = other_app.name if other_app else f"app #{existing_route.app_id}"
        return {
            "conflict":      True,
            "conflict_type": "local",
            "reason":        f"This hostname is already published by '{other_name}'. Publishing here will remove it from that app.",
            "owned_by":      other_name,
        }

    try:
        creds = _get_cf_creds(session)
        current_cfg = await _cf_request("GET", _cf_tunnel_cfg_url(creds), creds["cf_api_token"])
        result_obj  = current_cfg.get("result") or {}
        ingress     = result_obj.get("config", {}).get("ingress") or []

        for rule in ingress:
            if rule.get("hostname") == clean:
                current_service = rule.get("service", "unknown")
                server_ip = get_server_ipv4()
                check_port = local_port or app_row.port
                expected_service = f"http://{server_ip}:{check_port}" if check_port else None
                expected_service_legacy = f"http://localhost:{check_port}" if check_port else None
                if expected_service and (current_service == expected_service or current_service == expected_service_legacy):
                    return {"conflict": False}
                return {
                    "conflict":      True,
                    "conflict_type": "remote",
                    "reason":        f"This hostname is already in the tunnel config pointing to '{current_service}'. Publishing will overwrite it.",
                    "owned_by":      None,
                }
    except HTTPException:
        pass

    return {"conflict": False}


@router.post("/apps/{app_id}/tunnel/publish")
async def publish_cf_route(
    app_id: int,
    req: CfPublishReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Publish (or republish) an app route via Cloudflare Tunnel."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not req.local_port:
        raise HTTPException(400, "Local port must be specified for Cloudflare tunnel mapping.")
    if not (1 <= req.local_port <= 65535):
        raise HTTPException(400, "Local port must be between 1 and 65535.")

    new_hostname = _clean_cf_hostname(req.hostname)

    creds      = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    tunnel_id  = creds["cf_tunnel_id"]
    api_token  = creds["cf_api_token"]

    existing_route = session.exec(select(CfRoute).where(
        CfRoute.app_id == app_id,
        CfRoute.local_port == req.local_port
    )).first()
    old_hostname: Optional[str] = (
        existing_route.hostname
        if existing_route and existing_route.cf_route_id and existing_route.hostname
        else None
    )
    is_hostname_change = old_hostname is not None and old_hostname != new_hostname

    # The zone is looked up before anything is changed: a hostname on a
    # domain that is not in this Cloudflare account can't be published, and
    # finding that out first means there is nothing to undo.
    new_zone_id = await _cf_find_zone_id(new_hostname, account_id, api_token)
    if not new_zone_id:
        raise HTTPException(
            400,
            f"Domain '{new_hostname}' was not found in your Cloudflare account. "
            f"Make sure the root domain is added to Cloudflare and your API token "
            f"has Zone:Read + DNS:Edit permissions."
        )

    hostnames_to_strip = {new_hostname}
    if is_hostname_change:
        hostnames_to_strip.add(old_hostname)
    service = f"http://{get_server_ipv4()}:{req.local_port}"
    previous_ingress = await _cf_update_ingress(
        creds,
        lambda named: [r for r in named if r["hostname"] not in hostnames_to_strip]
        + [{"hostname": new_hostname, "service": service}],
    )

    try:
        dns_record_id = await _cf_upsert_dns_cname(new_zone_id, new_hostname, tunnel_id, api_token)
    except BaseException as exc:
        # Put the tunnel back exactly as it was — including the old hostname
        # of this route, which is still what the database says is published.
        logging.warning(f"DNS upsert failed for {new_hostname!r}, rolling back ingress: {exc}")
        try:
            await _cf_put_ingress(creds, previous_ingress)
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise

    # Only now that the new name works: drop the old one's DNS record.
    if is_hostname_change:
        try:
            old_zone_id = await _cf_find_zone_id(old_hostname, account_id, api_token)
            if old_zone_id:
                await _cf_delete_dns_cname(old_zone_id, old_hostname, api_token)
                logging.info(f"Removed old DNS CNAME: {old_hostname}")
        except Exception as exc:
            logging.warning(f"Could not delete old DNS CNAME for {old_hostname!r}: {exc}")

    # The tunnel has one rule per hostname, and it now points here. Any other
    # route that claimed this hostname — another app's, or this app's on a
    # different port — no longer exists.
    conflicting = session.exec(
        select(CfRoute).where(CfRoute.hostname == new_hostname)
    ).all()
    for c in conflicting:
        if existing_route is not None and c.id == existing_route.id:
            continue
        if c.app_id != app_id:
            other_app = session.get(App, c.app_id)
            if other_app and other_app.external_url == f"https://{new_hostname}":
                other_app.external_url = None
                session.add(other_app)
        c.cf_route_id = None
        c.hostname    = ""
        session.add(c)
        logging.info(f"Cleared stale CfRoute ownership of {new_hostname!r} from app {c.app_id}")

    route = existing_route or CfRoute(app_id=app_id, local_port=req.local_port)
    route.hostname    = new_hostname
    route.cf_route_id = new_hostname
    session.add(route)
    app_row.external_url = f"https://{new_hostname}"
    session.add(app_row)
    session.commit()
    audit_log(user.username, "tunnel_publish", f"app '{app_row.name}' (id={app_id}) -> {new_hostname}")

    return {
        "ok":            True,
        "hostname":      new_hostname,
        "url":           f"https://{new_hostname}",
        "dns_record_id": dns_record_id,
    }


@router.delete("/apps/{app_id}/tunnel/publish")
async def unpublish_cf_route(
    app_id: int,
    local_port: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Remove public hostname route mapping from Cloudflare tunnel and DNS record."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)

    route = session.exec(select(CfRoute).where(
        CfRoute.app_id == app_id,
        CfRoute.local_port == local_port
    )).first()

    if not route or not route.cf_route_id:
        raise HTTPException(400, "No published route found for this app.")

    creds      = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    api_token  = creds["cf_api_token"]
    hostname   = route.hostname

    await _cf_update_ingress(creds, lambda named: [r for r in named if r["hostname"] != hostname])

    try:
        zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
        if zone_id:
            await _cf_delete_dns_cname(zone_id, hostname, api_token)
            logging.info(f"DNS CNAME deleted for {hostname}")
        else:
            logging.warning(f"Zone not found for {hostname!r} during unpublish — DNS record may remain.")
    except Exception as exc:
        logging.warning(f"Could not delete DNS record for {hostname}: {exc}")

    route.cf_route_id = None
    route.hostname    = ""
    session.add(route)

    other_active = session.exec(select(CfRoute).where(
        CfRoute.app_id == app_id,
        CfRoute.id != route.id,
        CfRoute.cf_route_id != None
    )).first()

    # external_url may be the hostname that was just removed; point it at a
    # route that still exists rather than leaving a dead link behind.
    app_row.external_url = f"https://{other_active.hostname}" if other_active else None
    session.add(app_row)

    session.commit()
    audit_log(user.username, "tunnel_unpublish", f"app '{app_row.name}' (id={app_id})")
    return {"ok": True}
