"""Password hashing and strength rules.

Hashes are ``base64(salt || derived_key)``. New passwords use scrypt; the
verifier still accepts the 32-byte PBKDF2-SHA256 digests written by older
Venvy versions so that existing accounts keep working across an upgrade.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

__all__ = ["PasswordPolicyError", "hash_password", "validate_password_strength", "verify_password"]

_SALT_BYTES = 16
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
#: Length of the legacy PBKDF2 digest, used to tell the two schemes apart.
_LEGACY_PBKDF2_LEN = 32
_LEGACY_PBKDF2_ROUNDS = 100_000

MIN_PASSWORD_LENGTH = 8


class PasswordPolicyError(ValueError):
    """Raised when a proposed password does not meet the strength policy.

    The API layer converts this into a 400 response; keeping it a plain
    exception means the policy itself has no dependency on FastAPI.
    """


def hash_password(password: str) -> str:
    """Return a salted scrypt hash of *password*, encoded for storage."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=0
    )
    return base64.b64encode(salt + derived).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Check *plain* against a stored hash, in constant time.

    Returns ``False`` rather than raising for malformed or truncated hashes, so
    a corrupted row fails closed instead of 500-ing the login endpoint.
    """
    try:
        raw = base64.b64decode(hashed.encode("utf-8"))
        salt, derived = raw[:_SALT_BYTES], raw[_SALT_BYTES:]
        if not derived:
            return False
        if len(derived) == _LEGACY_PBKDF2_LEN:
            candidate = hashlib.pbkdf2_hmac(
                "sha256", plain.encode("utf-8"), salt, _LEGACY_PBKDF2_ROUNDS
            )
        else:
            candidate = hashlib.scrypt(
                plain.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=0
            )
        return secrets.compare_digest(derived, candidate)
    except Exception:
        return False


def validate_password_strength(password: str) -> None:
    """Raise :class:`PasswordPolicyError` if *password* is too weak.

    The rule is deliberately modest — length plus a letter and a digit. Venvy
    is a self-hosted tool where TOTP is the real second factor, and a policy
    strict enough to push users toward reuse would be a net loss.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
        )
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise PasswordPolicyError("Password must contain both letters and numbers.")
