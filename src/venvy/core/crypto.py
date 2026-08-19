"""Symmetric encryption for secrets held in the database.

Cloudflare API credentials and per-app environment variables are stored
encrypted at rest with a Fernet key kept in :attr:`Settings.key_file`. The key
never leaves the host, is never logged, and is never returned by the API.

Values written before encryption was introduced are stored as plain text; they
are recognised by the absence of the :data:`ENC_PREFIX` marker and returned
unchanged, so an existing database keeps working after an upgrade.
"""

from __future__ import annotations

import base64
import logging
import secrets

from cryptography.fernet import Fernet, InvalidToken

from venvy.config import settings

__all__ = ["ENC_PREFIX", "decrypt_secret", "encrypt_secret", "reset_cache"]

logger = logging.getLogger(__name__)

#: Marker distinguishing encrypted values from legacy plaintext ones.
ENC_PREFIX = "enc:v1:"

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return the process-wide Fernet, generating the key file on first run."""
    global _fernet
    if _fernet is not None:
        return _fernet

    key_file = settings.key_file
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_bytes(base64.urlsafe_b64encode(secrets.token_bytes(32)))

    # Re-assert 0600 on every startup: a restore from backup or a careless
    # `chmod -R` should not leave the key world-readable. Best-effort, since
    # some filesystems (and Windows) do not implement POSIX modes.
    try:
        key_file.chmod(0o600)
    except OSError:
        logger.debug("Could not chmod %s to 0600 — filesystem may not support it.", key_file)

    _fernet = Fernet(key_file.read_bytes())
    return _fernet


def reset_cache() -> None:
    """Forget the cached key so the next call re-reads :attr:`Settings.key_file`.

    Only needed by tests that switch data directories mid-process.
    """
    global _fernet
    _fernet = None


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* for storage.

    An empty string stays empty — there is nothing to protect, and keeping it
    empty lets callers use falsiness to mean "unset".
    """
    if not plaintext:
        return ""
    token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return ENC_PREFIX + token


def decrypt_secret(stored: str) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`.

    Legacy plaintext values (written before encryption existed) are returned
    unchanged. A value that cannot be decrypted yields an empty string and logs
    an error, which is what an operator sees when ``.venvy_key`` was lost or
    replaced without restoring the matching database.
    """
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        return stored
    token = stored[len(ENC_PREFIX) :]
    try:
        return _get_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        logger.error(
            "Failed to decrypt a stored secret — the .venvy_key file is missing or "
            "does not match this database. Restore the original key to recover it."
        )
        return ""
    except Exception:
        logger.exception("Unexpected error decrypting a stored secret.")
        return ""
