"""Authentication and request gating.

Passwords (scrypt), TOTP, sessions (an HttpOnly cookie plus a per-session
key sent as X-Venvy-Key), roles, one-time download tickets, the login and
mutation rate limiters, and working out which client a request came from.
"""
import base64
import datetime
import hashlib
import hmac
import re
import secrets
import struct
import threading
import time
import urllib.parse
from typing import Dict, Optional, Union

from fastapi import Depends, HTTPException, Request, Response
from sqlmodel import Session, select
from starlette.requests import HTTPConnection

from venvy.config import (
    IP_ALLOWLIST,
    PROXY_SECRET,
    SESSION_COOKIE_SECURE,
    SESSION_TTL_SECONDS,
    TRUSTED_PROXY_IPS,
    _make_tz_aware,
    utc_now,
)
from venvy.db import get_session
from venvy.models import ApiToken, AppAccess, User, UserSession


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
    code = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def _totp_verify(secret_b32: str, code: str, window: int = 1, after: Optional[int] = None) -> Optional[int]:
    """Return the time step *code* is valid for (current ± *window*), or None.

    Steps at or before *after* — the step of the last code this user was
    accepted with — don't count: a code is good for one use, so one that was
    shoulder-surfed or captured in transit can't be replayed inside the
    ~90 seconds it would otherwise stay valid (RFC 6238 §5.2)."""
    if not secret_b32 or not code:
        return None
    # Compared as bytes: compare_digest() raises TypeError on non-ASCII str,
    # which turned a mistyped code (e.g. full-width digits) into a 500.
    supplied = code.strip().encode("utf-8")
    now = int(time.time()) // 30
    for delta in range(-window, window + 1):
        if after is not None and now + delta <= after:
            continue
        expected = _totp_hotp(secret_b32, now + delta)
        if expected and hmac.compare_digest(expected.encode("ascii"), supplied):
            return now + delta
    return None


def _totp_uri(secret_b32: str, username: str, issuer: str = "Venvy") -> str:
    """Build the otpauth:// URI for QR code generation."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    params = urllib.parse.urlencode({
        "secret": secret_b32,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": 6,
        "period": 30,
    })
    return f"otpauth://totp/{label}?{params}"


def _resolve_client_ip(request: Union[Request, HTTPConnection]) -> str:
    """Resolve the real client IP.

    The TCP peer address is the only thing a client can't forge (uvicorn runs
    with proxy_headers=False, so X-Forwarded-For never rewrites it). When that
    peer is a trusted proxy — cloudflared on this host, by default — the
    request came through the tunnel and `CF-Connecting-IP`, which Cloudflare's
    edge sets and overwrites, names the actual client. From any other peer the
    header is ignored: a direct LAN/internet client could otherwise pick any
    IP it liked and walk past the IP allowlist and the login lockout.
    """
    peer = (request.client.host if request.client else "") or ""
    if peer in TRUSTED_PROXY_IPS:
        cf_ip = (request.headers.get("cf-connecting-ip", "") or "").strip()
        if cf_ip and (not PROXY_SECRET or secrets.compare_digest(
                (request.headers.get("x-venvy-proxy-secret", "") or "").encode("utf-8"),
                PROXY_SECRET.encode("utf-8"))):
            return cf_ip
    return peer or "unknown"


def check_ip_allowlist(request: HTTPConnection) -> None:
    """Dependency: reject requests from IPs not on the allowlist."""
    if IP_ALLOWLIST is None:
        return
    ip = _resolve_client_ip(request)
    if ip not in IP_ALLOWLIST:
        raise HTTPException(status_code=403, detail="Access from this IP is not allowed.")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=0)
    return base64.b64encode(salt + dk).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        raw = base64.b64decode(hashed.encode("utf-8"))
        salt = raw[:16]
        dk = raw[16:]
        if len(dk) == 32:
            check = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 100_000)
        else:
            check = hashlib.scrypt(plain.encode("utf-8"), salt=salt, n=16384, r=8, p=1, maxmem=0)
        return secrets.compare_digest(dk, check)
    except Exception:
        return False


# Checked when a login names a user that doesn't exist, so that case costs the
# same scrypt run as a wrong password and response time doesn't reveal which
# usernames are real.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise HTTPException(status_code=400, detail="Password must contain both letters and numbers.")


def _clean_username(username: str) -> str:
    name = username.strip()
    if not name or len(name) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise HTTPException(status_code=400, detail="Username must be 1–64 characters with no control characters.")
    return name


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_session(session: Session, response: Response, user: User) -> str:
    """Create a session for *user*: set the HttpOnly cookie and return the
    auth key the client must send back as X-Venvy-Key on every request.

    Why two secrets: browsers send cookies to every port on a host, so the
    session cookie also reaches each hosted app on http://<same-host>:<port>
    the moment you open it. The auth key lives in localStorage, which is
    scoped per origin (port included) and never sent automatically — a
    cookie captured by a hosted app is useless without it."""
    token = secrets.token_urlsafe(32)
    auth_key = secrets.token_urlsafe(32)
    session.add(UserSession(
        token=_hash_token(token),
        user_id=user.id,
        expires_at=utc_now() + datetime.timedelta(seconds=SESSION_TTL_SECONDS),
        key_hash=_hash_token(auth_key),
    ))
    session.commit()
    response.set_cookie(
        "session", token, httponly=True, path="/",
        max_age=SESSION_TTL_SECONDS, samesite="strict", secure=SESSION_COOKIE_SECURE,
    )
    return auth_key


def _load_session(session: Session, cookie_token: Optional[str]) -> Optional[UserSession]:
    """Return the live UserSession for a cookie value, sliding its expiry."""
    if not cookie_token:
        return None
    db_session = session.get(UserSession, _hash_token(cookie_token))
    if not db_session:
        return None
    now = utc_now()
    if now > _make_tz_aware(db_session.expires_at):
        session.delete(db_session)
        session.commit()
        return None
    # Slide expiry window if less than 7 days remain.
    remaining = (_make_tz_aware(db_session.expires_at) - now).total_seconds()
    if remaining < 60 * 60 * 24 * 7:
        db_session.expires_at = now + datetime.timedelta(seconds=SESSION_TTL_SECONDS)
        session.add(db_session)
        session.commit()
    return db_session


def _authenticate(session: Session, cookie_token: Optional[str], auth_key: Optional[str]) -> Optional[User]:
    db_session = _load_session(session, cookie_token)
    if not db_session or not db_session.key_hash or not auth_key:
        return None
    if not secrets.compare_digest(db_session.key_hash, _hash_token(auth_key)):
        return None
    return session.get(User, db_session.user_id)


TOKEN_PREFIX = "venvy_"
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_TOKEN_TOUCH_SEC = 300


def new_api_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def _authenticate_token(session: Session, request: Request, raw: str) -> User:
    """The user behind an `Authorization: Bearer venvy_...` token. Raises 401/403."""
    bad = HTTPException(status_code=401, detail="Invalid or expired API token.")
    if not raw.startswith(TOKEN_PREFIX):
        raise bad
    row = session.exec(select(ApiToken).where(ApiToken.token_hash == _hash_token(raw))).first()
    if not row:
        raise bad
    now = utc_now()
    if row.expires_at and now > _make_tz_aware(row.expires_at):
        raise bad
    user = session.get(User, row.user_id)
    if not user:
        raise bad
    if row.scope == "read" and request.method not in _SAFE_METHODS:
        raise HTTPException(status_code=403, detail="This API token is read-only.")
    if not row.last_used_at or (now - _make_tz_aware(row.last_used_at)).total_seconds() > _TOKEN_TOUCH_SEC:
        row.last_used_at = now
        session.add(row)
        session.commit()
    request.state.via_token = True
    return user


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    authz = request.headers.get("authorization", "")
    if authz[:7].lower() == "bearer ":
        # A token request never falls back to the cookie: it is judged on the token alone.
        return _authenticate_token(session, request, authz[7:].strip())
    user = _authenticate(session, request.cookies.get("session"), request.headers.get("x-venvy-key"))
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user


def get_session_user(request: Request, user: User = Depends(get_current_user)) -> User:
    """get_current_user, but not for API tokens: for the things a leaked token
    must not be able to do (make more tokens, change the password, switch 2FA off)."""
    if getattr(request.state, "via_token", False):
        raise HTTPException(status_code=403, detail="This needs a signed-in browser session, not an API token.")
    return user


def has_app_access(session: Session, user: User, app_id: int) -> bool:
    if user.role == "admin" or user.all_apps:
        return True
    return session.get(AppAccess, (user.id, app_id)) is not None


def visible_app_ids(session: Session, user: User) -> Optional[set]:
    """The ids of the apps the user may see, or None for all of them."""
    if user.role == "admin" or user.all_apps:
        return None
    return {a.app_id for a in session.exec(select(AppAccess).where(AppAccess.user_id == user.id)).all()}


def _check_path_app_access(request: Request, session: Session, user: User) -> None:
    raw = request.path_params.get("app_id")
    if raw is None:
        return
    try:
        app_id = int(raw)
    except (TypeError, ValueError):
        return
    if not has_app_access(session, user, app_id):
        raise HTTPException(status_code=404, detail="Not Found")


def require_app_access(request: Request, session: Session = Depends(get_session)) -> None:
    """Router-level dependency: a route with an {app_id} in its path is only for users who may use that app.
    404 rather than 403, so an app's existence is not revealed."""
    ticket = request.query_params.get("ticket")
    if ticket:
        # A download link. Look at whose ticket it is without using it up
        # (get_download_user does that). Any route takes ?ticket=, so a bad
        # one must not get a request past this check either.
        with _download_tickets_lock:
            entry = _download_tickets.get(_hash_token(ticket))
        if entry and entry[1] >= time.monotonic():
            user = session.get(User, entry[0])
            if user:
                _check_path_app_access(request, session, user)
                return
        try:
            user = get_current_user(request, session)
        except HTTPException:
            return        # not signed in either: the route's own check answers
        _check_path_app_access(request, session, user)
        return
    _check_path_app_access(request, session, get_current_user(request, session))


# One-time download tickets: a plain <a href download> link can't carry the
# X-Venvy-Key header, so the page trades its key for a ticket that works for
# one GET within a minute. {ticket_hash: (user_id, expires_monotonic)}
_download_tickets: Dict[str, tuple] = {}


_download_tickets_lock = threading.Lock()


_DOWNLOAD_TICKET_TTL = 60


def get_download_user(request: Request, session: Session = Depends(get_session)) -> User:
    """get_current_user for download endpoints: also accepts ?ticket=."""
    ticket = request.query_params.get("ticket")
    if not ticket:
        user = get_current_user(request, session)
        _check_path_app_access(request, session, user)
        return user
    now = time.monotonic()
    with _download_tickets_lock:
        for k in [k for k, (_, exp) in _download_tickets.items() if exp < now]:
            _download_tickets.pop(k, None)
        entry = _download_tickets.pop(_hash_token(ticket), None)
    user = session.get(User, entry[0]) if entry else None
    # The ticket must also come with this browser's session cookie.
    db_session = _load_session(session, request.cookies.get("session"))
    if not user or not db_session or db_session.user_id != user.id:
        raise HTTPException(status_code=401, detail="Download link expired — try again.")
    _check_path_app_access(request, session, user)
    return user


def check_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin privileges required.")
    return user


def require_operator(user: User = Depends(get_current_user)) -> User:
    """Dependency for any endpoint that mutates apps (start/stop/delete/edit files/
    packages/tunnels). Admins and operators may proceed; viewers are read-only."""
    if user.role not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="Operator or admin privileges required.")
    return user


# ---------------------------------------------------------------------------
# Auth routes  /  Login rate limiting
# ---------------------------------------------------------------------------
_login_attempts: dict = {}


_login_lock = threading.Lock()


_LOGIN_MAX_TRIES   = 5


_LOGIN_LOCKOUT_SEC = 300


# Failures are also counted per username, whatever address they come from —
# the only limit that holds against a client able to vary its apparent IP
# (see PROXY_SECRET). Higher than the per-IP limit so that ordinary typos
# from one place trip that one first.
_LOGIN_MAX_TRIES_PER_USER = 20


_LOGIN_MAX_TRACKED = 50_000


def _login_rate_key(request: Request) -> str:
    return _resolve_client_ip(request)


def _check_login_rate(key: str) -> None:
    with _login_lock:
        entry = _login_attempts.get(key)
        if not entry:
            return
        locked_until = entry.get('locked_until')
        if locked_until and time.time() < locked_until:
            remaining = int(locked_until - time.time()) + 1
            raise HTTPException(
                429,
                f'Too many failed attempts. Try again in {remaining} seconds.',
                headers={'Retry-After': str(remaining), 'X-Retry-After': str(remaining)},
            )


def _login_user_key(username: str) -> str:
    return "user:" + username.strip().lower()[:128]


def _record_login_failure(key: str, max_tries: int = _LOGIN_MAX_TRIES) -> None:
    with _login_lock:
        if key not in _login_attempts and len(_login_attempts) >= _LOGIN_MAX_TRACKED:
            return
        entry = _login_attempts.setdefault(key, {'count': 0, 'locked_until': None})
        entry['count'] += 1
        entry['last'] = time.time()
        if entry['count'] >= max_tries:
            entry['locked_until'] = time.time() + _LOGIN_LOCKOUT_SEC
            entry['count'] = 0


def _login_gate(key: str, max_tries: int = _LOGIN_MAX_TRIES):
    """For password prompts other than the login form (changing the
    password, switching 2FA off): returns fail(), to be called when the
    password was wrong, after answering 429 if *key* is locked out. A
    signed-in session must not be a way to guess the password at leisure."""
    _check_login_rate(key)

    def fail() -> None:
        _record_login_failure(key, max_tries)
        _check_login_rate(key)

    return fail


def _clear_login_rate(key: str) -> None:
    with _login_lock:
        _login_attempts.pop(key, None)


def _purge_login_attempts() -> None:
    """Drop entries that no longer matter (called periodically). A failure
    count is only forgotten once it has been quiet for a full lockout period:
    this used to drop every not-yet-locked entry on each sweep, which reset
    the count every two minutes and let a client guess indefinitely at four
    tries per sweep without ever being locked out."""
    now = time.time()
    with _login_lock:
        for key, entry in list(_login_attempts.items()):
            locked_until = entry.get('locked_until')
            if locked_until:
                expired = now > locked_until + _LOGIN_LOCKOUT_SEC
            else:
                expired = now > entry.get('last', 0) + _LOGIN_LOCKOUT_SEC
            if expired:
                _login_attempts.pop(key, None)


# ---------------------------------------------------------------------------
# General per-IP rate limiting for app-mutating endpoints
# ---------------------------------------------------------------------------
# Same sliding-window-of-attempts pattern as the login limiter above, but scoped
# to a generous ceiling meant to catch a compromised/buggy frontend session or
# script hammering the host, not to throttle normal interactive use. The file
# manager sends one request per item, so selecting a few hundred files and
# deleting or moving them has to fit under it.
_mutation_hits: dict = {}


_mutation_lock = threading.Lock()


_MUTATION_MAX_PER_WINDOW = 600


_MUTATION_WINDOW_SEC = 60


def rate_limit_mutations(request: Request) -> None:
    """Dependency: raise 429 if this IP has made too many mutating requests recently."""
    key = _resolve_client_ip(request)
    now = time.time()
    with _mutation_lock:
        hits = _mutation_hits.setdefault(key, [])
        cutoff = now - _MUTATION_WINDOW_SEC
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= _MUTATION_MAX_PER_WINDOW:
            raise HTTPException(
                429,
                "Too many requests. Please slow down.",
                headers={"Retry-After": str(_MUTATION_WINDOW_SEC), "X-Retry-After": str(_MUTATION_WINDOW_SEC)},
            )
        hits.append(now)


def _purge_mutation_hits() -> None:
    """Forget clients whose window has emptied (called periodically). Without
    it the table kept one entry for every address that ever made a request."""
    cutoff = time.time() - _MUTATION_WINDOW_SEC
    with _mutation_lock:
        for key in [k for k, hits in _mutation_hits.items() if not hits or hits[-1] < cutoff]:
            _mutation_hits.pop(key, None)
