"""Cloudflare routes: credential checks, zones, and per-app tunnel routes."""
import asyncio
import json
import logging
import urllib.parse
from typing import Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlmodel import select, Session

from venvy import own_hosts, ports
from venvy.audit import audit_log
from venvy.auth import (
    check_admin,
    check_ip_allowlist,
    get_current_user,
    has_app_access,
    rate_limit_mutations,
    require_app_access,
    require_operator,
)
from venvy.cloudflare import (
    _cf_delete_dns_cname,
    _cf_find_zone_id,
    _cf_request,
    _cf_tunnel_cfg_url,
    _cf_update_ingress,
    _cf_upsert_dns_cname,
    _clean_cf_hostname,
    _get_cf_creds,
    _sanitize_cf_value,
)
from venvy.crypto import _decrypt_secret
from venvy.db import engine, get_session
from venvy.models import App, CfRoute, SystemSetting, User
from venvy.process import get_server_ipv4

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist), Depends(require_app_access)]
)


@router.post("/system/cloudflare/test")
async def test_cf_credentials(
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
    _rl: None = Depends(rate_limit_mutations),
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
    account_id = creds["cf_account_id"]

    zones: List[Dict[str, str]] = []
    page        = 1
    per_page    = 50

    async def _fetch_zones():
        nonlocal page
        while True:
            url = (
                f"https://api.cloudflare.com/client/v4/zones"
                f"?per_page={per_page}&page={page}&status=active"
                f"&account.id={urllib.parse.quote(account_id, safe='')}"
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


def _app_ports(app_row: App, seen: bool = True) -> Set[int]:
    """The ports that are this app's (venvy.ports.claimed)."""
    return ports.claimed(app_row, seen)


def _venvy_port(request: Request) -> Optional[int]:
    """The port Venvy itself is listening on (the one this request came in on)."""
    server = request.scope.get("server")
    return server[1] if server and len(server) > 1 else None


def _service_port(service: str) -> Optional[int]:
    try:
        return urllib.parse.urlsplit(service or "").port
    except ValueError:
        return None


def _publish_refusal(rule: Optional[dict], hostname: str, published: Set[str], user: User,
                     own_port: Optional[int]) -> Optional[tuple]:
    """(status, reason) when *hostname* must not be (re)pointed at an app, else None.
    *rule* is the tunnel's current rule for it, *published* the hostnames Venvy's
    apps have published. Never Venvy's own address; and a hostname the tunnel
    sends somewhere Venvy did not set up (another service on this host or
    elsewhere) is only an admin's to take over."""
    if own_hosts.is_own(hostname) or (rule and own_port and _service_port(rule.get("service", "")) == own_port
                                      and hostname not in published):
        return 409, f"{hostname} is an address Venvy itself is reached under; an app cannot take it over."
    if rule and hostname not in published and user.role != "admin":
        return 403, (f"{hostname} is already in the tunnel, pointing to '{rule.get('service', 'unknown')}', "
                     "and was not published from Venvy. Only an admin can take it over.")
    return None


def _any_host_listener_on_port(port: int) -> bool:
    """True if anything on the host listens on TCP *port* (venvy.ports.in_use)."""
    return ports.in_use(port)


async def _check_publish_port(session: Session, app_row: App, port: int, request: Request) -> None:
    """Only a port that is this app's goes on the internet from here. Otherwise
    any port on the host could: another app's (one this user may not even have
    access to), or a database that listens on localhost only because it trusts
    this host. The app's port is a setting anyone who can publish can change,
    so it also must not be another app's, nor something else's that is
    listening on it now."""
    if not (1 <= port <= 65535):
        raise HTTPException(400, "Local port must be between 1 and 65535.")
    if port == _venvy_port(request):
        raise HTTPException(400, "That is Venvy's own port; an app cannot publish it.")
    if port not in ports.claimed(app_row):
        raise HTTPException(400, f"Port {port} is not one of this app's ports. Set it as the app's port first.")
    if port in ports.claimed_by_apps(session, skip_app_id=app_row.id):
        raise HTTPException(409, f"Port {port} is also another app's port.")
    # Whoever listens there now (not the stored detected_ports: with ports set by
    # hand, those are what it holds). A docker / compose app's ports are Docker's
    # to listen on, not its CLI process's: ports.holder knows.
    if await asyncio.to_thread(ports.holder, port, app_row) == "other":
        raise HTTPException(409, f"Something other than this app is listening on port {port}.")


def _check_route_owner(session: Session, hostname: str, app_id: int, user: User) -> None:
    """A hostname another app has published moves to this one only for a user
    who may use that other app too."""
    for r in session.exec(select(CfRoute).where(CfRoute.hostname == hostname, CfRoute.app_id != app_id)).all():
        if r.cf_route_id and not has_app_access(session, user, r.app_id):
            raise HTTPException(403, f"{hostname} is published by an app you do not have access to.")


def _published_hostnames(session: Session) -> Set[str]:
    return {r.hostname for r in session.exec(select(CfRoute).where(CfRoute.cf_route_id != None)).all()  # noqa: E711
            if r.hostname}


@router.get("/apps/{app_id}/tunnel/check")
async def check_cf_hostname(
    app_id: int,
    request: Request,
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
    if own_hosts.is_own(clean):
        return {"conflict": True, "conflict_type": "venvy", "blocked": True,
                "reason": f"{clean} is an address Venvy itself is reached under; an app cannot take it over."}

    try:
        _check_route_owner(session, clean, app_id, user)
    except HTTPException as exc:
        return {"conflict": True, "conflict_type": "local", "blocked": True, "reason": exc.detail, "owned_by": None}

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
            if (rule.get("hostname") or "").lower() == clean:
                refusal = _publish_refusal(rule, clean, _published_hostnames(session), user, _venvy_port(request))
                if refusal:
                    return {"conflict": True, "conflict_type": "remote", "blocked": True,
                            "reason": refusal[1], "owned_by": None}
                current_service = rule.get("service", "unknown")
                server_ip = get_server_ipv4()
                check_port = local_port or app_row.port
                expected_service = f"http://{server_ip}:{check_port}" if check_port else None
                expected_service_legacy = f"http://localhost:{check_port}" if check_port else None
                via_venvy = f"http://localhost:{_venvy_port(request)}"
                own_route = any(r.app_id == app_id and (r.hostname or "").lower() == clean
                                for r in session.exec(select(CfRoute).where(CfRoute.app_id == app_id)).all())
                if (expected_service and (current_service == expected_service or current_service == expected_service_legacy)) \
                        or (own_route and current_service == via_venvy):
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
    request: Request,
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
    refusal = _publish_refusal(None, new_hostname, set(), user, None)
    if refusal:
        raise HTTPException(*refusal)
    _check_route_owner(session, new_hostname, app_id, user)
    await _check_publish_port(session, app_row, req.local_port, request)

    creds      = _get_cf_creds(session)
    account_id = creds["cf_account_id"]
    tunnel_id  = creds["cf_tunnel_id"]
    api_token  = creds["cf_api_token"]
    app_name   = app_row.name

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
    # Release the DB connection before external Cloudflare HTTP calls.
    session.close()

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

    hostnames_to_strip = {new_hostname.lower()}
    if is_hostname_change and old_hostname:
        hostnames_to_strip.add(old_hostname.lower())
    own_port = _venvy_port(request)
    if app_row.instances > 1 or app_row.zero_downtime:
        # Through Venvy's proxy, which knows the hostname (the route above) and
        # spreads requests over the replicas, keeping them up through restarts.
        service = f"http://localhost:{own_port}"
    elif not app_row.bind_public:
        service = f"http://localhost:{req.local_port}"      # a private app listens on loopback only
    else:
        service = f"http://{get_server_ipv4()}:{req.local_port}"

    prior_new_rule: List[dict] = []
    prior_old_rule: List[dict] = []

    def change(named: List[dict]) -> List[dict]:
        # Re-checked under the ingress lock against fresh DB state:
        # raising here leaves the tunnel untouched.
        with Session(engine) as s:
            _check_route_owner(s, new_hostname, app_id, user)
            published = {h.lower() for h in _published_hostnames(s)}
        current = next((r for r in named if (r.get("hostname") or "").lower() == new_hostname.lower()), None)
        refused = _publish_refusal(current, new_hostname.lower(), published, user, own_port)
        if refused:
            raise HTTPException(*refused)
        prior_new_rule[:] = [current] if current else []
        if is_hostname_change and old_hostname:
            old_r = next((r for r in named if (r.get("hostname") or "").lower() == old_hostname.lower()), None)
            prior_old_rule[:] = [old_r] if old_r else []
        return [r for r in named if (r.get("hostname") or "").lower() not in hostnames_to_strip] + [{"hostname": new_hostname, "service": service}]

    await _cf_update_ingress(creds, change)

    try:
        dns_record_id = await _cf_upsert_dns_cname(new_zone_id, new_hostname, tunnel_id, api_token)
    except BaseException as exc:
        # Surgically undo our change under the ingress lock so concurrent
        # tunnel edits from other requests are not overwritten.
        logging.warning(f"DNS upsert failed for {new_hostname!r}, rolling back ingress: {exc}")
        try:
            def _undo(named: List[dict]) -> List[dict]:
                restored = [r for r in named if (r.get("hostname") or "").lower() != new_hostname.lower()]
                if prior_new_rule:
                    restored.append(prior_new_rule[0])
                if is_hostname_change and old_hostname and prior_old_rule and not any((r.get("hostname") or "").lower() == old_hostname.lower() for r in restored):
                    restored.append(prior_old_rule[0])
                return restored
            await _cf_update_ingress(creds, _undo)
        except Exception as rb_exc:
            logging.warning(f"Ingress rollback failed: {rb_exc}")
        raise

    # Only now that the new name works: drop the old one's DNS record.
    if is_hostname_change and old_hostname:
        try:
            old_zone_id = await _cf_find_zone_id(old_hostname, account_id, api_token)
            if old_zone_id:
                await _cf_delete_dns_cname(old_zone_id, old_hostname, api_token, tunnel_id=tunnel_id)
                logging.info(f"Removed old DNS CNAME: {old_hostname}")
        except Exception as exc:
            logging.warning(f"Could not delete old DNS CNAME for {old_hostname!r}: {exc}")

    # The tunnel has one rule per hostname, and it now points here. Any other
    # route that claimed this hostname — another app's, or this app's on a
    # different port — no longer exists.
    with Session(engine) as s:
        existing_route = s.exec(select(CfRoute).where(
            CfRoute.app_id == app_id,
            CfRoute.local_port == req.local_port,
        )).first()
        conflicting = s.exec(
            select(CfRoute).where(CfRoute.hostname == new_hostname)
        ).all()
        for c in conflicting:
            if existing_route is not None and c.id == existing_route.id:
                continue
            if c.app_id != app_id:
                other_app = s.get(App, c.app_id)
                if other_app and other_app.external_url == f"https://{new_hostname}":
                    other_app.external_url = None
                    s.add(other_app)
            c.cf_route_id = None
            c.hostname    = ""
            s.add(c)
            logging.info(f"Cleared stale CfRoute ownership of {new_hostname!r} from app {c.app_id}")

        route = existing_route or CfRoute(app_id=app_id, local_port=req.local_port)
        route.hostname    = new_hostname
        route.cf_route_id = new_hostname
        s.add(route)
        fresh_app = s.get(App, app_id)
        if fresh_app:
            fresh_app.external_url = f"https://{new_hostname}"
            s.add(fresh_app)
        s.commit()
    audit_log(user.username, "tunnel_publish", f"app '{app_name}' (id={app_id}) -> {new_hostname}")

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
    tunnel_id  = creds["cf_tunnel_id"]
    api_token  = creds["cf_api_token"]
    hostname   = route.hostname
    route_id   = route.id
    app_name   = app_row.name
    session.close()

    await _cf_update_ingress(
        creds,
        lambda named: [r for r in named if (r.get("hostname") or "").lower() != hostname.lower()],
    )

    try:
        zone_id = await _cf_find_zone_id(hostname, account_id, api_token)
        if zone_id:
            await _cf_delete_dns_cname(zone_id, hostname, api_token, tunnel_id=tunnel_id)
            logging.info(f"DNS CNAME deleted for {hostname}")
        else:
            logging.warning(f"Zone not found for {hostname!r} during unpublish — DNS record may remain.")
    except Exception as exc:
        logging.warning(f"Could not delete DNS record for {hostname}: {exc}")

    with Session(engine) as s:
        fresh_route = s.get(CfRoute, route_id)
        if fresh_route:
            fresh_route.cf_route_id = None
            fresh_route.hostname    = ""
            s.add(fresh_route)
        other_active = s.exec(select(CfRoute).where(
            CfRoute.app_id == app_id,
            CfRoute.id != route_id,
            CfRoute.cf_route_id != None
        )).first()
        fresh_app = s.get(App, app_id)
        if fresh_app:
            # external_url may be the hostname that was just removed; point it at a
            # route that still exists rather than leaving a dead link behind.
            fresh_app.external_url = f"https://{other_active.hostname}" if other_active else None
            s.add(fresh_app)
        s.commit()
    audit_log(user.username, "tunnel_unpublish", f"app '{app_name}' (id={app_id})")
    return {"ok": True}
