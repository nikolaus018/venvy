"""In-process rate limiters for login attempts and state-changing requests.

State lives in memory, keyed by client IP. That is the right trade-off for a
single-host self-hosted tool: no Redis to run, and a restart clearing the
counters is acceptable because sessions survive it anyway.

Both limiters are thread-safe — sync FastAPI route handlers run in a thread
pool, so several requests can reach them at once.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request, status

from venvy.config import settings
from venvy.security.dependencies import resolve_client_ip

__all__ = [
    "LoginRateLimiter",
    "login_limiter",
    "rate_limit_mutations",
    "reset_all",
]


@dataclass
class _LoginAttempts:
    """Failed-login bookkeeping for one client."""

    count: int = 0
    #: Unix timestamp until which this client is locked out, or None.
    locked_until: float | None = None
    last_seen: float = field(default_factory=time.time)


class LoginRateLimiter:
    """Locks a client out after repeated failed sign-ins.

    Counts failures per IP; on reaching the threshold the client is locked out
    for a fixed window and the counter resets, so a locked-out client is not
    permanently locked by continuing to try.
    """

    def __init__(self) -> None:
        self._attempts: dict[str, _LoginAttempts] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Raise 429 if *key* is currently locked out."""
        with self._lock:
            entry = self._attempts.get(key)
            if not entry or not entry.locked_until:
                return
            remaining = int(entry.locked_until - time.time()) + 1
            if remaining <= 0:
                return
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many failed attempts. Try again in {remaining} seconds.",
            headers={"Retry-After": str(remaining), "X-Retry-After": str(remaining)},
        )

    def record_failure(self, key: str) -> int:
        """Register a failed attempt. Returns seconds locked out, 0 if not."""
        with self._lock:
            entry = self._attempts.setdefault(key, _LoginAttempts())
            entry.count += 1
            entry.last_seen = time.time()
            if entry.count >= settings.login_max_attempts:
                entry.locked_until = time.time() + settings.login_lockout_seconds
                entry.count = 0
                return settings.login_lockout_seconds
            return 0

    def clear(self, key: str) -> None:
        """Forget *key* entirely, called on a successful sign-in."""
        with self._lock:
            self._attempts.pop(key, None)

    def purge_expired(self) -> int:
        """Drop entries whose lockout lapsed long ago. Returns how many went.

        Called periodically by the supervisor so the map cannot grow without
        bound when Venvy is exposed to background scanning traffic.
        """
        cutoff = time.time() - 300
        with self._lock:
            stale = [
                key
                for key, entry in self._attempts.items()
                if (entry.locked_until or entry.last_seen) < cutoff
            ]
            for key in stale:
                self._attempts.pop(key, None)
        return len(stale)

    def reset(self) -> None:
        """Drop all state. For tests."""
        with self._lock:
            self._attempts.clear()


#: The process-wide login limiter.
login_limiter = LoginRateLimiter()


class _MutationRateLimiter:
    """Sliding-window cap on state-changing requests per IP.

    The ceiling is generous by design: it exists to stop a runaway script or a
    compromised session from hammering the host, not to throttle a person
    clicking around the UI.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        """Raise 429 if *key* has exceeded its allowance."""
        now = time.time()
        window = settings.mutation_window_seconds
        with self._lock:
            hits = self._hits.setdefault(key, [])
            cutoff = now - window
            # The list is append-only in timestamp order, so dropping the
            # expired prefix is enough to age the window.
            fresh_from = 0
            while fresh_from < len(hits) and hits[fresh_from] < cutoff:
                fresh_from += 1
            if fresh_from:
                del hits[:fresh_from]
            if len(hits) >= settings.mutation_max_per_window:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Too many requests. Please slow down.",
                    headers={"Retry-After": str(window), "X-Retry-After": str(window)},
                )
            hits.append(now)

    def purge_expired(self) -> None:
        """Drop IPs with no recent activity."""
        cutoff = time.time() - settings.mutation_window_seconds
        with self._lock:
            for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
                self._hits.pop(key, None)

    def reset(self) -> None:
        """Drop all state. For tests."""
        with self._lock:
            self._hits.clear()


_mutation_limiter = _MutationRateLimiter()


def rate_limit_mutations(request: Request) -> None:
    """Dependency applied to every state-changing endpoint."""
    _mutation_limiter.check(resolve_client_ip(request))


def purge_expired() -> None:
    """Housekeeping hook called by the supervisor loop."""
    login_limiter.purge_expired()
    _mutation_limiter.purge_expired()


def reset_all() -> None:
    """Clear both limiters. For tests."""
    login_limiter.reset()
    _mutation_limiter.reset()
