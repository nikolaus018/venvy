"""Secrets-at-rest encryption.

Cloudflare credentials, TOTP secrets and each app's
environment variables are stored in data.db encrypted with a symmetric key
kept in DATA_DIR/.venvy_key. Without that file those columns are
unrecoverable — back it up alongside data.db and treat it like a private key.
"""
import base64
import contextlib
import logging
import os
import secrets
import threading
from typing import Optional

from cryptography.fernet import Fernet

from venvy.config import _VENVY_KEY_FILE


_ENC_PREFIX = "enc:v1:"


_fernet_instance: Optional[Fernet] = None


_fernet_lock = threading.Lock()


def _get_fernet() -> Fernet:
    """Return a process-wide Fernet instance, generating the local key on first run.

    The key never leaves this host, is never logged, and is never returned in
    any API response. Back it up alongside data.db (see module docstring).
    """
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    # Request handlers run in a thread pool, so first use can be concurrent.
    # Unlocked, two threads could each generate a key: one encrypts with a key
    # the other then overwrites on disk, and that ciphertext is lost for good.
    with _fernet_lock:
        if _fernet_instance is None:
            if not _VENVY_KEY_FILE.exists():
                # Born 0600 under a private name, then linked into place: the
                # key is never readable by others (it used to be created with
                # the umask's mode and chmod'ed afterwards) and never exists
                # half-written. link() refuses to replace an existing key.
                tmp = _VENVY_KEY_FILE.with_name(f"{_VENVY_KEY_FILE.name}.{secrets.token_hex(4)}.tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(base64.urlsafe_b64encode(secrets.token_bytes(32)))
                        f.flush()
                        os.fsync(f.fileno())
                    with contextlib.suppress(FileExistsError):
                        os.link(tmp, _VENVY_KEY_FILE)
                finally:
                    tmp.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                _VENVY_KEY_FILE.chmod(0o600)
            _fernet_instance = Fernet(_VENVY_KEY_FILE.read_bytes())
    return _fernet_instance


def _encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* for storage. Empty string stays empty (nothing to protect)."""
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENC_PREFIX + token


def _try_decrypt_secret(stored: str) -> tuple:
    """(ok, plaintext) for a value written by _encrypt_secret. Tolerates legacy
    plaintext values (rows written before encryption was introduced) by
    returning them unchanged. ok is False only when a value is there but
    can't be decrypted — which callers must not mistake for "empty"."""
    if not stored:
        return True, ""
    if not stored.startswith(_ENC_PREFIX):
        return True, stored
    token = stored[len(_ENC_PREFIX):]
    try:
        return True, _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        logging.error("Failed to decrypt a stored secret — the .venvy_key file may be missing or changed.")
        return False, ""


def _decrypt_secret(stored: str) -> str:
    """Plaintext of a stored secret, or "" if it is empty or unreadable."""
    return _try_decrypt_secret(stored)[1]
