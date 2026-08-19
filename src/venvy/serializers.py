"""Conversion of ORM rows into the JSON shapes the API returns."""

from __future__ import annotations

import datetime

from venvy.core.crypto import decrypt_secret
from venvy.core.net import get_server_ipv4
from venvy.db.models import App, require_id
from venvy.services import registry

__all__ = ["serialize_app"]

#: Datetime columns rendered as ISO-8601 strings.
_DATETIME_FIELDS = (
    "created_at",
    "updated_at",
    "last_start_time",
    "last_stop_time",
    "last_manually_stopped_at",
)


def serialize_app(app: App, reveal_secrets: bool = True) -> dict:
    """Render *app* for the API, adding derived URLs and live metrics.

    ``env_json`` holds real secrets for the hosted app — database passwords,
    API keys — encrypted at rest. Pass ``reveal_secrets=False`` for callers
    whose role should not see them (a viewer browsing the app list); the field
    then comes back empty with ``env_redacted`` set, so the UI can say "there
    are variables here, you just cannot read them" rather than "there are none".
    """
    data = app.model_dump()

    if reveal_secrets:
        data["env_json"] = decrypt_secret(app.env_json) or "{}"
    else:
        data["env_json"] = "{}"
        data["env_redacted"] = bool(app.env_json)

    local_url = f"http://{get_server_ipv4()}:{app.port}" if app.port else None
    data["local_url"] = local_url
    data["metrics"] = registry.app_metrics.get(require_id(app.id), {"cpu": 0.0, "memory": 0})

    # access_url is what the UI links to. The user's preference wins when a
    # public URL actually exists; otherwise it falls back to the LAN address.
    if app.url_source_pref == "public" and app.external_url:
        data["access_url"] = app.external_url
        data["url_source"] = "cloudflare"
    elif local_url:
        data["access_url"] = local_url
        data["url_source"] = "local"
    else:
        data["access_url"] = None
        data["url_source"] = None

    for field in _DATETIME_FIELDS:
        value = data.get(field)
        if isinstance(value, datetime.datetime):
            data[field] = value.isoformat()

    return data
