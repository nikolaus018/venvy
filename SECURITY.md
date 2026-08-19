# Security

## Reporting a vulnerability

Please report privately, not in a public issue.

Use [GitHub's private vulnerability reporting](https://github.com/nikolaus018/venvy/security/advisories/new)
on this repository. Include what you did, what happened, and what you expected —
a proof of concept helps but is not required.

Expect an acknowledgement within a few days. Venvy is maintained by one person,
so please allow reasonable time for a fix before disclosing publicly.

## Supported versions

The latest release on `main` is the supported version.

---

## What Venvy is

**Venvy is an administrative surface that runs arbitrary code on the host.**
That is not a weakness; it is the entire feature set. An operator can upload a
program, install packages, edit files and open a shell. It follows that:

> Operator access to Venvy is equivalent to shell access on the host, as the
> user Venvy runs as. Grant it accordingly.

Venvy is designed for a machine you own, reached over a private network or a
Cloudflare Tunnel. It is not built to be exposed to the open internet, and it
is not a multi-tenant platform — there is no isolation between apps beyond
their resource limits.

## The trust boundary

| | |
|---|---|
| **Trusted** | The host, the OS user Venvy runs as, and anyone with admin or operator credentials. |
| **Semi-trusted** | Viewers. They are read-only, and cannot read app environment variables. |
| **Untrusted** | Everything arriving over HTTP before authentication: request bodies, headers, paths, filenames, uploads. |

## What is enforced

**Authentication.** Passwords are scrypt-hashed with a per-user salt (older
PBKDF2-SHA256 hashes still verify, so upgrades do not lock anyone out). TOTP
two-factor auth is available per account. Sessions are opaque 256-bit tokens in
an `HttpOnly`, `SameSite=Lax` cookie, and slide forward while in use.

**Authorisation.** Three roles, checked as FastAPI dependencies on every route
rather than in the handler bodies. The matrix is in the README and is covered
endpoint by endpoint in `tests/test_api_authz.py`.

**Brute force.** Failed sign-ins are counted per client IP and lock that client
out for five minutes after five failures. State-changing endpoints have a
separate, generous per-IP ceiling aimed at runaway scripts.

**CSRF.** Session auth rides on a cookie, so every state-changing `/api/`
request must echo the `csrf_token` cookie in an `x-csrf-token` header —
something only same-origin JavaScript can do.

**Client IP resolution.** Only `CF-Connecting-IP` is trusted, because
Cloudflare's edge overwrites it. `X-Forwarded-For` is deliberately ignored:
Cloudflare only *appends* to it, so its first entry is attacker-controlled and
honouring it would let anyone bypass the IP allowlist and the login lockout by
setting a header. There is a test for this.

**Path containment.** Every user-supplied path resolves through
`venvy.core.paths.is_safe_path`, which fully resolves both sides — so symlinks
pointing out of the app directory are rejected rather than followed. The jail
support files cannot be written through the file API.

**Secrets at rest.** App environment variables and Cloudflare credentials are
Fernet-encrypted in the database with a key in `.venvy_key` (mode `0600`). The
key is never logged and never returned by the API. Values are redacted from API
responses for viewers.

**Command construction.** Nothing is passed through a shell. Package names are
additionally matched against strict allowlist patterns — defence in depth, and
a clearer error than a wall of tool output.

**Transport.** Conservative security headers on every response, including a
CSP. `SESSION_COOKIE_SECURE=1` should be set whenever Venvy is served over
HTTPS.

## Known limitations

These are design trade-offs, stated plainly rather than left to be discovered.

**The terminal jail is a usability guardrail, not a sandbox.** The overridden
`cd` and the `PROMPT_COMMAND` guard stop accidental wandering. They do not stop
anyone who wants out: the shell runs as Venvy's own user, and any program it
launches has that user's full access. Real isolation needs a container or a
dedicated unprivileged user per app.

**Apps are not isolated from each other.** They share a user and a filesystem.
Resource limits are enforced (via systemd), access is not.

**`unsafe-inline` is in the CSP.** Tailwind's CDN build and the inline PWA
bootstrap both require it. Self-hosting the vendor bundle removes the need —
see `docs/security.md`.

**Vendor libraries load from public CDNs.** Convenient, but it means trusting
those origins and requires outbound network access. Vendor them locally for a
hardened or air-gapped deployment.

**Rate-limit state is in memory.** It resets when Venvy restarts, and is
per-process. Right for a single-host tool; not a distributed rate limiter.

**No CSRF on the WebSocket handshake.** The terminal socket authenticates from
the session cookie. `SameSite=Lax` and the `frame-ancestors 'none'` CSP are
what stand between that and a cross-site WebSocket.

## Hardening checklist

- [ ] Run Venvy as a dedicated unprivileged user, never root.
- [ ] Bind to `127.0.0.1` and reach it through a tunnel or reverse proxy.
- [ ] Set `SESSION_COOKIE_SECURE=1` behind HTTPS.
- [ ] Set `IP_ALLOWLIST` if the set of clients is known.
- [ ] Turn on TOTP for every account.
- [ ] Give people `viewer` unless they genuinely need to change things.
- [ ] Back up `.venvy_key` with `data.db`, and keep both off public storage.
- [ ] Leave `VENVY_AUTO_INSTALL_SYSTEM_DEPS` off; install toolchains yourself.
- [ ] Ship the audit log (`AUDIT:` lines) somewhere durable.
