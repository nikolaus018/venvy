"""Environment variables every app gets (an app's own variables win)."""
import json
import logging
from typing import Dict

from sqlmodel import Session

from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.models import SystemSetting

KEY = "global_env"


def load() -> Dict[str, str]:
    """The shared variables ({} if none, or if they cannot be decrypted)."""
    try:
        with Session(engine) as s:
            row = s.get(SystemSetting, KEY)
        if not row or not row.value:
            return {}
        ok, plain = _try_decrypt_secret(row.value)
        data = json.loads(plain) if ok and plain else {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception as exc:
        logging.warning(f"Could not read the shared environment variables: {exc}")
        return {}


def readable() -> bool:
    with Session(engine) as s:
        row = s.get(SystemSetting, KEY)
    return True if not row or not row.value else _try_decrypt_secret(row.value)[0]


def save(values: Dict[str, str]) -> None:
    with Session(engine) as s:
        row = s.get(SystemSetting, KEY) or SystemSetting(key=KEY)
        row.value = _encrypt_secret(json.dumps(values))
        s.add(row)
        s.commit()
