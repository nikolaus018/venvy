"""HTTP hardening middleware: security headers and CSRF protection."""

from __future__ import annotations

import secrets

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from venvy.config import settings

__all__ = ["CSRF_COOKIE", "CSRF_HEADER", "CSRFMiddleware", "SecurityHeadersMiddleware"]

CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "x-csrf-token"

#: Methods that must not change state, and so need no CSRF token.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: The frontend pulls Tailwind, Ace, xterm, JSZip, Feather and QRCode from
#: public CDNs, so those origins have to be allowed. 'unsafe-inline' is
#: required by Tailwind's runtime compiler and by the inline bootstrap that
#: generates the PWA icons; self-hosting the vendor bundle is the way to drop
#: both — see docs/security.md.
_CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com "
        "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://unpkg.com",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
        "font-src 'self' https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self' ws: wss: https://cdn.jsdelivr.net https://cdnjs.cloudflare.com",
        "worker-src blob:",
        "manifest-src 'self' blob:",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    )
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Content-Security-Policy": _CSP,
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach conservative security headers to every response.

    Uses ``setdefault`` so a route that deliberately sets its own value — a
    download that needs different framing rules, say — is not overridden.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


class CSRFMiddleware(BaseHTTPMiddleware):
    """Double-submit-cookie CSRF protection for the JSON API.

    Session auth rides on a cookie, so a cross-site form post would otherwise
    be attached to the user's session. Every state-changing ``/api/`` request
    must therefore echo the ``csrf_token`` cookie back in a header — something
    only same-origin JavaScript can read and do.

    The cookie is intentionally *not* ``HttpOnly``: the frontend has to read it
    to set the header. It carries no authority of its own; possession only
    proves the caller can read a same-origin cookie.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith("/api/") and request.method.upper() not in _SAFE_METHODS:
            cookie_token = request.cookies.get(CSRF_COOKIE)
            header_token = request.headers.get(CSRF_HEADER)
            if (
                not cookie_token
                or not header_token
                or not secrets.compare_digest(cookie_token, header_token)
            ):
                return JSONResponse(
                    {"detail": "CSRF validation failed"},
                    status_code=403,
                )

        response = await call_next(request)

        if CSRF_COOKIE not in request.cookies:
            response.set_cookie(
                CSRF_COOKIE,
                secrets.token_urlsafe(32),
                httponly=False,
                samesite="lax",
                path="/",
                secure=settings.session_cookie_secure,
            )
        return response
