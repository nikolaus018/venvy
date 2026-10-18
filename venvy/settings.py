"""Rules for the system settings key-value store: what is secret, what is
reserved, what is encrypted at rest."""
from fastapi import HTTPException

from venvy.cloudflare import _sanitize_cf_value
from venvy.crypto import _encrypt_secret


# Kept by their own API (encrypted JSON), never through the generic one.
_INTERNAL_KEYS = ("global_env", "backup_remote")


def _is_secret_setting(key: str) -> bool:
    """Settings whose value is never returned by the API once stored."""
    return key == "cf_api_token" or key.startswith("totp_pending_") or key in _INTERNAL_KEYS


def _reject_reserved_setting(key: str) -> None:
    """Keys Venvy keeps its own state under are not settable through the API."""
    if key.startswith("totp_pending_") or key in _INTERNAL_KEYS:
        raise HTTPException(400, f"'{key}' is reserved.")


def _setting_for_storage(key: str, value: str) -> tuple:
    """(cleaned value, value as stored) for a system setting — the one place
    that decides which settings are encrypted at rest."""
    if key.startswith("cf_"):
        value = _sanitize_cf_value(value)
        return value, _encrypt_secret(value)
    return value, value
