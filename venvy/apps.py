"""App helpers shared by the API: naming rules and the API representation."""
import datetime
import re

from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect

from venvy import monitor
from venvy.crypto import _try_decrypt_secret
from venvy.isolation import _isolation_active
from venvy.models import App
from venvy.process import _app_metrics, _systemd_run_available, get_server_ipv4


def _clean_app_name(name: str) -> str:
    name_clean = name.strip()
    if not name_clean:
        raise HTTPException(status_code=400, detail="App name cannot be empty.")
    if len(name_clean) > 50:
        raise HTTPException(status_code=400, detail="App name must be 50 characters or less.")
    return name_clean


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def sanitize_slug(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[\s\W-]+", "-", s)
    return s or "app"


def _app_safe(app_row: App, reveal_secrets: bool = True) -> dict:
    """Return App as dict — injects local_url + access_url + live metrics.

    env_json is encrypted at rest and holds real secrets (DB passwords, API
    keys, etc. for the hosted app). reveal_secrets=False redacts it instead
    of decrypting, for callers where the requester's role shouldn't see
    plaintext env vars (e.g. a read-only viewer browsing the app list)."""
    # A commit expires the row, and model_dump() reads the instance's dict
    # without reloading: after list_apps() marked a crashed app stopped, the
    # app came back with no id, name or status at all.
    state = sa_inspect(app_row)
    if state.expired_attributes and state.session is not None:
        state.session.refresh(app_row)
    d = app_row.model_dump()
    # Never leave the server: these are credentials for the repository and the push hook.
    d["git_configured"] = bool(d.get("git_url"))
    d["git_has_token"] = bool(d.pop("git_auth", None))
    d.pop("webhook_secret", None)
    d["health"] = monitor.health_info(app_row.id) if app_row.healthcheck_path else None
    d["disk_bytes"] = monitor.cached_disk(app_row.id)
    if reveal_secrets:
        env_ok, env_plain = _try_decrypt_secret(app_row.env_json)
        d["env_json"] = env_plain or "{}"
        # Stored but undecryptable (key lost or replaced). The UI must not
        # present this as "no variables" and save that back over them.
        d["env_unreadable"] = not env_ok
    else:
        d["env_json"] = "{}"
        d["env_redacted"] = bool(app_row.env_json)
    local_url = f"http://{get_server_ipv4()}:{app_row.port}" if app_row.port else None
    d["local_url"] = local_url
    d["metrics"]   = _app_metrics.get(app_row.id, {"cpu": 0.0, "memory": 0})
    d["resource_limits_enforced"] = _systemd_run_available()
    # Whether the app, its terminal and its tools run as its own Unix user.
    d["isolated"] = _isolation_active()
    pref = app_row.url_source_pref
    if pref == "public" and app_row.external_url:
        d["access_url"] = app_row.external_url
        d["url_source"] = "cloudflare"
    elif local_url:
        d["access_url"] = local_url
        d["url_source"] = "local"
    else:
        d["access_url"] = None
        d["url_source"] = None
    for key in ("created_at", "updated_at", "last_start_time", "last_stop_time",
                "last_manually_stopped_at"):
        val = d.get(key)
        if isinstance(val, datetime.datetime):
            d[key] = val.isoformat()
    return d
