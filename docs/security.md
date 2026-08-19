# Security model

This document is the detailed companion to [SECURITY.md](../SECURITY.md),
which holds the reporting process and the summary. Read that first.

## The one thing to understand

Venvy runs arbitrary code on the host, by design. Uploading a program,
installing packages, editing files and opening a shell are the product, not
loopholes in it.

> **Operator access to Venvy is equivalent to shell access on the host, as the
> user Venvy runs as.**

Everything below follows from that. The security work in Venvy is about
protecting the boundary *around* that capability — who gets in, and what an
unauthenticated request can reach — not about constraining an operator who is
already inside.

## Assets

| Asset | Where it lives | Protection |
|---|---|---|
| Account credentials | `user.password_hash` | scrypt, per-user salt |
| TOTP secrets | `user.totp_secret` | plaintext column; the database is the boundary |
| Session tokens | `usersession.token` | 256-bit, opaque, `HttpOnly` cookie |
| App environment variables | `app.env_json` | Fernet, key in `.venvy_key` |
| Cloudflare credentials | `systemsetting.cf_*` | Fernet, key in `.venvy_key` |
| The encryption key | `.venvy_key` | mode `0600`, never logged, never in an API response |
| App source and data | `apps/` | filesystem permissions (`0750`) |

## Controls, and what each is actually for

### Authentication

Passwords are scrypt-hashed (`n=16384, r=8, p=1`) with a 16-byte random salt.
The verifier also accepts 32-byte PBKDF2-SHA256 digests written by older
versions, so an upgrade does not lock anyone out. Verification is constant-time
and a malformed hash returns `False` rather than raising, so a corrupted row
fails closed instead of returning a 500 from the login endpoint.

TOTP is RFC 6238 (SHA-1, 6 digits, 30 s) implemented on the standard library,
with a plus-or-minus one step window for clock skew and constant-time
comparison. Enrolment is two-phase: the secret is parked in a settings row and
only becomes active once the user proves their authenticator produces a
matching code.

The login response deliberately does not distinguish "no such user" from "wrong
password" — same status, same detail. There is a test asserting this.

### Sessions

Opaque 256-bit tokens, server-side records, `HttpOnly` plus `SameSite=Lax` plus
`Secure` (when configured). Thirty-day lifetime that slides forward when fewer
than seven days remain, so an actively used session never expires under the
user. Sessions are deleted on logout, on expiry (by the supervisor), and when
the owning account is deleted.

### Authorisation

Three roles, enforced as FastAPI dependencies on the route rather than inside
handler bodies — so a new endpoint that forgets its dependency has no
authorisation at all rather than partial authorisation, which is the more
obvious failure. `tests/test_api_authz.py` checks the matrix endpoint by
endpoint rather than sampling it.

Viewers additionally get app environment variables redacted at serialisation
time, with an `env_redacted` flag so the UI can say "there are variables here,
you cannot read them" rather than "there are none".

### Client IP resolution

Only `CF-Connecting-IP` is trusted. Cloudflare's edge sets that header itself
and overwrites whatever the client sent, so it cannot be forged from outside.

`X-Forwarded-For` is deliberately ignored. Cloudflare only *appends* to it, so
its first entry remains attacker-controlled — honouring it would let anyone
bypass both the IP allowlist and the login lockout by setting a header. Without
a Cloudflare header, Venvy falls back to the raw TCP peer, which also cannot be
spoofed. There is a test for this exact bypass.

The consequence, stated plainly: behind a *non-Cloudflare* reverse proxy, every
request appears to come from the proxy. Enforce client restrictions at the
proxy in that topology.

### CSRF

Double-submit cookie. Every state-changing `/api/` request must echo the
`csrf_token` cookie in an `x-csrf-token` header. The cookie is intentionally
not `HttpOnly` — the frontend has to read it — and carries no authority of its
own; possession only proves the caller can read a same-origin cookie.

### Rate limiting

Login failures are counted per client IP: five failures locks that IP out for
five minutes, and the counter resets on lockout so a locked client is not
permanently locked by continuing to try. A successful sign-in clears it.

State-changing endpoints share a separate sliding window (60 per minute per IP
by default). That ceiling is generous on purpose: it exists to stop a runaway
script or a compromised session, not to throttle someone clicking around.

Both limiters are in-process and thread-safe, and both are purged periodically
by the supervisor so the maps cannot grow without bound under background
scanning traffic.

### Path containment

Every user-supplied path goes through `venvy.core.paths.is_safe_path`, which
fully resolves both the root and the target before comparing — so a symlink
pointing out of the app directory is rejected rather than followed. Both ends
of a rename or copy are checked, not just the source. The jail support files
(`.jailrc*`, `.bashrc-venvy`) cannot be written or renamed through the file
API, because rewriting them would defeat the terminal jail.

Uploaded filenames are matched against an allowlist pattern and must be plain
basenames. Entrypoints must be bare filenames, never paths.

### Command construction

Nothing is passed through a shell — every subprocess is
`create_subprocess_exec` with an argv list. Package names are additionally
matched against strict patterns; that is defence in depth rather than the only
barrier, and it also produces a clear error instead of a wall of tool output.

## Known limitations

Stated here rather than left to be discovered.

**The terminal jail is a guardrail, not a sandbox.** The overridden `cd` and
the `PROMPT_COMMAND` guard prevent accidental wandering. They do not stop
anyone who wants out: the shell runs as Venvy's user, and any program it
launches has that user's full access. Real isolation requires a container or a
dedicated unprivileged user per app.

**Apps are not isolated from one another.** They share a user and a filesystem.
Resource limits are enforced through systemd scopes; access is not.

**TOTP secrets are stored in plaintext.** Unlike app environment variables and
Cloudflare credentials, they are not encrypted at rest. Anyone with the
database file can mint valid codes — though they would still need the password.

**`unsafe-inline` is in the CSP.** Required by Tailwind's CDN build and the
inline PWA bootstrap. Self-hosting the vendor bundle removes the need.

**Vendor libraries load from public CDNs.** That means trusting those origins
and requiring outbound network access. Vendor them locally for a hardened or
air-gapped deployment, and tighten the CSP accordingly.

**Rate-limit state is in memory.** It resets on restart and is per-process.
Correct for a single-host tool; not a distributed rate limiter.

**The WebSocket handshake has no CSRF check.** The terminal socket
authenticates from the session cookie. `SameSite=Lax` and
`frame-ancestors 'none'` are what stand between that and a cross-site
WebSocket.

**No account lockout, only IP lockout.** An attacker distributing attempts
across many source addresses is not slowed by the login limiter. TOTP is the
mitigation.

## Hardening checklist

- [ ] Run as a dedicated unprivileged user, never root.
- [ ] Bind to `127.0.0.1`; reach it through a tunnel or reverse proxy.
- [ ] `SESSION_COOKIE_SECURE=1` behind HTTPS.
- [ ] `IP_ALLOWLIST` when the client set is known.
- [ ] TOTP on every account.
- [ ] `viewer` unless someone genuinely needs to change things.
- [ ] Back up `.venvy_key` with `data.db`; keep both off shared storage.
- [ ] Leave `VENVY_AUTO_INSTALL_SYSTEM_DEPS` off.
- [ ] Ship `AUDIT:` log lines somewhere durable.
- [ ] Review published Cloudflare hostnames periodically — they are internet-facing.
