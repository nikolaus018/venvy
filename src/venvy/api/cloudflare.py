"""Publishing apps to the internet through a Cloudflare Tunnel.

Publishing touches two independent Cloudflare resources — the tunnel's ingress
rules and the zone's DNS records — with no transaction spanning them. The
publish flow therefore rolls the ingress change back by hand whenever the DNS
half fails, so the tunnel is never left routing a hostname that resolves
nowhere.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import Session, col, select

from venvy.core.audit import audit_log
from venvy.core.net import get_server_ipv4
from venvy.db.engine import get_session
from venvy.db.models import App, CfRoute, User
from venvy.schemas import CfPublishRequest
from venvy.security.dependencies import get_current_user, require_operator
from venvy.security.rate_limit import rate_limit_mutations
from venvy.services import cloudflare as cf

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps/{app_id}/tunnel", tags=["cloudflare"])


def _get_app_or_404(session: Session, app_id: int) -> App:
    """Fetch an app or raise 404."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


def _service_url(port: int) -> str:
    """The local address the tunnel should forward to for *port*."""
    return f"http://{get_server_ipv4()}:{port}"


@router.get("")
def list_cf_routes(
    app_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """List the hostnames configured for this app."""
    _get_app_or_404(session, app_id)
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


@router.get("/check")
async def check_cf_hostname(
    app_id: int,
    hostname: str = Query(...),
    local_port: int | None = Query(None),
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Pre-flight a hostname and report any conflict.

    Two kinds exist. A *local* conflict means another Venvy app already claims
    the hostname; a *remote* one means the tunnel already routes it somewhere
    else — often a previous deployment. Either way publishing would take it
    over, so the UI asks first rather than silently stealing it.

    Never fails the request: an unreachable Cloudflare means we cannot check
    remotely, which is not a reason to block the user from trying.
    """
    app = _get_app_or_404(session, app_id)
    clean = cf.normalize_hostname(hostname)
    if not clean:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid hostname.")

    existing = session.exec(select(CfRoute).where(CfRoute.hostname == clean)).first()
    if existing and existing.app_id != app_id and existing.cf_route_id:
        other = session.get(App, existing.app_id)
        other_name = other.name if other else f"app #{existing.app_id}"
        return {
            "conflict": True,
            "conflict_type": "local",
            "reason": (
                f"This hostname is already published by '{other_name}'. "
                f"Publishing here will remove it from that app."
            ),
            "owned_by": other_name,
        }

    try:
        credentials = cf.get_credentials(session)
        ingress = await cf.get_tunnel_config(
            credentials.account_id, credentials.tunnel_id, credentials.api_token
        )
    except HTTPException:
        return {"conflict": False}

    port = local_port or app.port
    for rule in ingress:
        if rule.get("hostname") != clean:
            continue
        current = rule.get("service", "unknown")
        # Accept the legacy localhost form too — older Venvy versions wrote it,
        # and it points at the same place.
        expected = {_service_url(port), f"http://localhost:{port}"} if port else set()
        if current in expected:
            return {"conflict": False}
        return {
            "conflict": True,
            "conflict_type": "remote",
            "reason": (
                f"This hostname is already in the tunnel configuration, pointing to "
                f"'{current}'. Publishing will overwrite it."
            ),
            "owned_by": None,
        }

    return {"conflict": False}


async def _rollback_ingress(
    credentials: cf.CloudflareCredentials, original: list[dict], drop: set[str]
) -> None:
    """Restore the ingress list after a failed publish.

    Best-effort: if the rollback itself fails there is nothing further to try
    automatically, so it is logged loudly for the operator.
    """
    try:
        await cf.put_tunnel_ingress(
            credentials.account_id,
            credentials.tunnel_id,
            credentials.api_token,
            cf.build_ingress(original, drop),
        )
    except Exception as exc:
        logger.warning("Ingress rollback failed — the tunnel may carry a stale rule: %s", exc)


def _release_conflicting_routes(session: Session, hostname: str, app_id: int) -> None:
    """Detach *hostname* from any other app that still claims it."""
    for route in session.exec(select(CfRoute).where(CfRoute.hostname == hostname)).all():
        if route.app_id == app_id:
            continue
        other = session.get(App, route.app_id)
        if other and other.external_url == f"https://{hostname}":
            other.external_url = None
            session.add(other)
        route.cf_route_id = None
        route.hostname = ""
        session.add(route)
        logger.info("Released stale claim on %r from app %s", hostname, route.app_id)


@router.put("/publish")
async def publish_cf_route(
    app_id: int,
    request: CfPublishRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Publish one of the app's ports at a public hostname."""
    app = _get_app_or_404(session, app_id)

    hostname = cf.normalize_hostname(request.hostname)
    if not hostname or "." not in hostname:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invalid hostname — it must contain at least one dot (e.g. app.example.com).",
        )

    credentials = cf.get_credentials(session)

    existing_route = session.exec(
        select(CfRoute).where(CfRoute.app_id == app_id, CfRoute.local_port == request.local_port)
    ).first()
    old_hostname = (
        existing_route.hostname
        if existing_route and existing_route.cf_route_id and existing_route.hostname
        else None
    )
    is_rename = old_hostname is not None and old_hostname != hostname

    original_ingress = await cf.get_tunnel_config(
        credentials.account_id, credentials.tunnel_id, credentials.api_token
    )

    drop = {hostname}
    if is_rename and old_hostname is not None:
        drop.add(old_hostname)

    await cf.put_tunnel_ingress(
        credentials.account_id,
        credentials.tunnel_id,
        credentials.api_token,
        cf.build_ingress(
            original_ingress,
            drop,
            {"hostname": hostname, "service": _service_url(request.local_port)},
        ),
    )

    if is_rename and old_hostname is not None:
        try:
            old_zone = await cf.find_zone_id(
                old_hostname, credentials.account_id, credentials.api_token
            )
            if old_zone:
                await cf.delete_dns_cname(old_zone, old_hostname, credentials.api_token)
                logger.info("Removed the previous DNS record for %s", old_hostname)
        except Exception as exc:
            logger.warning("Could not remove the old DNS record for %r: %s", old_hostname, exc)

    try:
        zone_id = await cf.find_zone_id(hostname, credentials.account_id, credentials.api_token)
    except HTTPException:
        await _rollback_ingress(credentials, original_ingress, drop)
        raise

    if not zone_id:
        await _rollback_ingress(credentials, original_ingress, drop)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"The domain for '{hostname}' was not found in your Cloudflare account. "
            f"Check that the root domain is added to Cloudflare and that the API token "
            f"has Zone:Read and DNS:Edit permissions.",
        )

    try:
        dns_record_id = await cf.upsert_dns_cname(
            zone_id, hostname, credentials.tunnel_id, credentials.api_token
        )
    except HTTPException:
        await _rollback_ingress(credentials, original_ingress, drop)
        raise

    _release_conflicting_routes(session, hostname, app_id)

    route = existing_route or CfRoute(app_id=app_id, local_port=request.local_port)
    route.hostname = hostname
    # The hostname doubles as the route identifier: tunnel ingress rules have no
    # ids of their own, so the hostname is what identifies the rule to remove.
    route.cf_route_id = hostname
    session.add(route)

    app.external_url = f"https://{hostname}"
    session.add(app)
    session.commit()

    audit_log(user.username, "tunnel_publish", f"'{app.name}' (id={app_id}) -> {hostname}")
    return {
        "ok": True,
        "hostname": hostname,
        "url": f"https://{hostname}",
        "dns_record_id": dns_record_id,
    }


@router.delete("/publish")
async def unpublish_cf_route(
    app_id: int,
    local_port: int = Query(...),
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Remove a published hostname from the tunnel and delete its DNS record."""
    app = _get_app_or_404(session, app_id)

    route = session.exec(
        select(CfRoute).where(CfRoute.app_id == app_id, CfRoute.local_port == local_port)
    ).first()
    if not route or not route.cf_route_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "No published route was found for that port."
        )

    credentials = cf.get_credentials(session)
    hostname = route.hostname

    ingress = await cf.get_tunnel_config(
        credentials.account_id, credentials.tunnel_id, credentials.api_token
    )
    await cf.put_tunnel_ingress(
        credentials.account_id,
        credentials.tunnel_id,
        credentials.api_token,
        cf.build_ingress(ingress, {hostname}),
    )

    # The ingress rule is already gone, so the route is unreachable either way;
    # a failure to tidy the DNS record must not fail the request.
    try:
        zone_id = await cf.find_zone_id(hostname, credentials.account_id, credentials.api_token)
        if zone_id:
            await cf.delete_dns_cname(zone_id, hostname, credentials.api_token)
            logger.info("DNS record deleted for %s", hostname)
        else:
            logger.warning(
                "Zone not found for %r while unpublishing — its DNS record may remain.",
                hostname,
            )
    except Exception as exc:
        logger.warning("Could not delete the DNS record for %s: %s", hostname, exc)

    route.cf_route_id = None
    route.hostname = ""
    session.add(route)

    still_published = session.exec(
        select(CfRoute).where(CfRoute.app_id == app_id, col(CfRoute.cf_route_id).is_not(None))
    ).first()
    if not still_published:
        app.external_url = None
        session.add(app)

    session.commit()
    audit_log(user.username, "tunnel_unpublish", f"'{app.name}' (id={app_id}) -> {hostname}")
    return {"ok": True}
