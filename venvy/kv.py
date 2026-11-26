"""Venvy's own settings (the systemsetting table), plain or encrypted, as text or JSON."""
import json
from typing import Any

from sqlmodel import Session

from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.models import SystemSetting


def get(key: str, default: str = "") -> str:
    with Session(engine) as s:
        row = s.get(SystemSetting, key)
    return row.value if row and row.value is not None else default


def put(key: str, value: str) -> None:
    with Session(engine) as s:
        row = s.get(SystemSetting, key) or SystemSetting(key=key)
        row.value = value
        s.add(row)
        s.commit()


def get_json(key: str, default: Any = None, secret: bool = False) -> Any:
    raw = get(key, "")
    if not raw:
        return default
    if secret:
        ok, raw = _try_decrypt_secret(raw)
        if not ok:
            return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def put_json(key: str, value: Any, secret: bool = False) -> None:
    raw = json.dumps(value)
    put(key, _encrypt_secret(raw) if secret else raw)
