import base64
import datetime
import hashlib
import hmac
import re
import secrets
import struct
import time
import urllib.parse

from fastapi import Depends, HTTPException, Request
from sqlmodel import Session

# TOTP — uses only stdlib (hmac, hashlib, struct, time, base64)
# No external dependency required.


def _totp_generate_secret() -> str:
    """Generate a random 20-byte base32 secret for TOTP."""
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("utf-8")


def _totp_hotp(secret_b32: str, counter: int) -> str:
    """Compute a 6-digit HOTP value."""
    try:
        key = base64.b32decode(secret_b32.upper().replace(" ", ""))
    except Exception:
        return ""
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _totp_verify(secret_b32: str, code: str, window: int = 1) -> bool:
    """Return True if *code* matches the current TOTP ± *window* steps."""
    if not secret_b32 or not code:
        return False
    now = int(time.time()) // 30
    for delta in range(-window, window + 1):
        if hmac.compare_digest(_totp_hotp(secret_b32, now + delta), code.strip()):
            return True
    return False


def _totp_uri(secret_b32: str, username: str, issuer: str = "Venvy") -> str:
    """Build the otpauth:// URI for QR code generation."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    params = urllib.parse.urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": 6,
            "period": 30,
        }
    )
    return f"otpauth://totp/{label}?{params}"


from app.config import (
    SESSION_TTL_SECONDS,
    SESSION_COOKIE_SECURE,
)
from app.database import User, UserSession, get_session, utc_now

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=0
    )
    return base64.b64encode(salt + dk).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        raw = base64.b64decode(hashed.encode("utf-8"))
        salt = raw[:16]
        dk = raw[16:]
        if len(dk) == 32:
            check = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 100_000)
        else:
            check = hashlib.scrypt(
                plain.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=0
            )
        return secrets.compare_digest(dk, check)
    except Exception:
        return False


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(
            status_code=400, detail="Password must be at least 8 characters long."
        )
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise HTTPException(
            status_code=400, detail="Password must contain both letters and numbers."
        )


def _make_tz_aware(dt: datetime.datetime) -> datetime.datetime:
    """Return *dt* with UTC tzinfo attached if it is naive."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.timezone.utc)


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated.")

    db_session = session.get(UserSession, token)
    if not db_session:
        raise HTTPException(status_code=401, detail="Session invalid.")

    now = utc_now()
    if now > _make_tz_aware(db_session.expires_at):
        session.delete(db_session)
        session.commit()
        raise HTTPException(status_code=401, detail="Session expired.")

    # Slide expiry window if less than 7 days remain.
    remaining = (_make_tz_aware(db_session.expires_at) - now).total_seconds()
    if remaining < 60 * 60 * 24 * 7:
        db_session.expires_at = now + datetime.timedelta(seconds=SESSION_TTL_SECONDS)
        session.add(db_session)
        session.commit()

    user = session.get(User, db_session.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found.")
    return user


def check_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return user
