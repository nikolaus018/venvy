"""RFC 6238 time-based one-time passwords.

Implemented against the standard library alone (``hmac``, ``hashlib``,
``struct``, ``base64``) so that enabling two-factor auth costs no extra
dependency. Compatible with Google Authenticator, Aegis, 1Password and any
other SHA-1 / 6-digit / 30-second authenticator.
"""

from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time
import urllib.parse

__all__ = ["DIGITS", "PERIOD_SECONDS", "generate_secret", "provisioning_uri", "verify"]

#: Number of digits in a generated code.
DIGITS = 6
#: Length of a code's validity window.
PERIOD_SECONDS = 30
#: Secret size. 20 bytes is the RFC 4226 recommendation for HMAC-SHA1.
_SECRET_BYTES = 20


def generate_secret() -> str:
    """Return a fresh random base32 secret suitable for an authenticator app."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("utf-8")


def _hotp(secret_b32: str, counter: int) -> str:
    """Compute the HOTP code for *counter* against a base32 secret."""
    # Authenticator apps often display secrets without padding; restore it so
    # b32decode does not reject an otherwise valid secret pasted by a user.
    padded = secret_b32 + "=" * (-len(secret_b32) % 8)
    key = base64.b32decode(padded, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), "sha1").digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**DIGITS)).zfill(DIGITS)


def verify(secret_b32: str, code: str, window: int = 1) -> bool:
    """Check *code* against *secret_b32*.

    *window* counts how many 30-second steps either side of now are accepted,
    which absorbs clock skew between the server and the user's phone. The
    comparison is constant-time so a code cannot be recovered by timing.
    """
    if not secret_b32 or not code:
        return False
    cleaned = code.strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != DIGITS:
        return False
    counter = int(time.time()) // PERIOD_SECONDS
    for drift in range(-window, window + 1):
        try:
            expected = _hotp(secret_b32, counter + drift)
        except Exception:
            return False
        if hmac.compare_digest(expected, cleaned):
            return True
    return False


def provisioning_uri(secret_b32: str, username: str, issuer: str = "Venvy") -> str:
    """Build the ``otpauth://`` URI an authenticator app scans as a QR code."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    params = urllib.parse.urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{params}"
