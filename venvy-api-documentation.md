# Venvy API Documentation

**Version:** 0.1.0 (server build v4.0.0)
**Base URL:** `http://<host>:<port>/api` (default `http://127.0.0.1:54783`)
**Protocol:** REST over HTTP/1.1, JSON request/response bodies unless noted
**Auth:** Session cookie (server-issued), CSRF double-submit token

---

## 1. Overview

Venvy is a self-hosted "premium application manager" — a control plane for running, isolating, and exposing multiple Python/Rust applications from a single host. The API lets a client:

- Authenticate and manage user accounts (admin/operator/viewer roles)
- Upload, configure, start/stop/restart, and delete hosted apps
- Browse, edit, upload, and download app files in a sandboxed directory
- Manage each app's Python (pip) or Rust (cargo) dependencies
- Stream/tail app logs
- Inspect host-level metrics and take database backups
- Publish apps publicly through a Cloudflare Tunnel + DNS
- Open an interactive terminal into an app's environment (WebSocket)

All endpoints except `GET /api/health` and `GET /` live under `/api` and are subject to an optional IP allowlist, session authentication, and CSRF protection (see §2–3).

---

## 2. Authentication

### 2.1 Session model

Venvy uses **server-side sessions**. Every authenticated request needs **two** secrets:

| Secret | Where it lives | Purpose |
|---|---|---|
| `session` cookie | `HttpOnly`, `SameSite=Strict`, `Secure` (if `SESSION_COOKIE_SECURE=1`), 30-day max-age | Identifies the session |
| `X-Venvy-Key` header | returned as `auth_key` by login/setup; the web UI keeps it in `localStorage` | Proves the request comes from the Venvy UI's own origin |
| `csrf_token` cookie | `SameSite=Lax`, **not** HttpOnly, `Secure` if configured | Double-submit CSRF token, auto-issued on first response |

Why the header: browsers send cookies to *every port* on a host, so the `session` cookie also reaches each hosted app opened at `http://<same-host>:<port>`. `localStorage` is per origin (port included) and never sent automatically, so a hosted app that captures the cookie still can't use it. A request with the cookie but no (or a wrong) key gets `401`.

Both secrets are stored only as SHA-256 hashes in `data.db`. A session is valid for 30 days from issuance and **slides forward** automatically: any authenticated request extends `expires_at` to a fresh 30 days if fewer than 7 days remain. Changing your password signs out all of your other sessions.

**Downloads.** A plain `<a download>` link can't send the header, so the client first calls `POST /api/auth/download-ticket` and appends the returned ticket as `?ticket=` to the download URL. Tickets are single-use, expire after 60 s, and only work together with the same user's session cookie.

### 2.2 First-run setup

The server has no users on first boot. `GET /api/auth/status` reports `user_exists: false` until the initial admin is created via `POST /api/auth/setup`. Only one account may be created this way — the endpoint returns `400` on every call thereafter.

### 2.3 Login flow

```
1. Client → POST /api/auth/login { username, password }
2. If the account has TOTP enabled and no totp_code was sent → 403 "totp_required"
3. Client retries with totp_code included
4. Server sets the "session" cookie and returns { ok, username, role, auth_key }
5. Client sends auth_key as the X-Venvy-Key header on every later request
```

Failed logins are rate-limited **per client IP** (not per account): 5 failures locks that IP out for 5 minutes (`429` with an `X-Retry-After` header). A successful login clears the counter.

### 2.4 CSRF protection

Every mutating request (`POST`, `PUT`, `DELETE`, `PATCH` — anything but `GET`/`HEAD`/`OPTIONS`/`TRACE`) under `/api/` must echo the `csrf_token` cookie value back in an `X-CSRF-Token` header. Requests that omit or mismatch it receive `403 {"detail": "CSRF validation failed"}` before reaching the route handler. Clients should read the cookie (it's not `HttpOnly`) and attach it to every state-changing call.

### 2.5 Two-factor authentication (TOTP)

Standard RFC 6238 TOTP (SHA-1, 6 digits, 30-second step), compatible with any authenticator app. Setup is two steps: begin (issue a pending secret) → confirm (verify a code to activate it). Disabling requires re-entering the current password.

### 2.6 CORS

`CORSMiddleware` is configured with `allow_credentials=True` and `allow_origins` set from the `ALLOWED_ORIGINS` environment variable (comma-separated). **If unset, `allow_origins` is an empty list — the API accepts same-origin requests only.** A frontend served from a different origin (e.g. a separate dev server on another port, or a CDN-hosted SPA) will have every request blocked by the browser unless the operator sets `ALLOWED_ORIGINS` to include that origin. There's no wildcard fallback (`allow_credentials=True` makes `"*"` invalid under the CORS spec anyway).

### 2.7 IP allowlisting

If the server is started with the `IP_ALLOWLIST` environment variable set (comma-separated IPs), every `/api/*` request (and the terminal WebSocket) is checked against it. Requests from IPs off the list get `403`.

How the client IP is determined (also used by the login lockout and rate limits): the raw TCP peer address. `X-Forwarded-For` is never used (uvicorn runs with `proxy_headers=False`). `CF-Connecting-IP` is honoured **only** when the TCP peer is in `TRUSTED_PROXY_IPS` (default `127.0.0.1,::1`, i.e. `cloudflared` on the same host), because only then did the request really pass through Cloudflare. If `cloudflared` runs on a different machine, add that machine's IP to `TRUSTED_PROXY_IPS`.

An address alone can't tell `cloudflared` from other software on the same host: with the default setting, a hosted app can connect over loopback and send its own `CF-Connecting-IP`. To close that, set `VENVY_PROXY_SECRET` and add a Cloudflare request-header transform rule that sends the same value as `X-Venvy-Proxy-Secret`; the header is then only believed on requests that carry the secret (it is stripped from hosted apps' environments). Independently of this, failed logins are also limited per username (20 failures → 5-minute lock), so varying the apparent IP does not allow unlimited password guessing.

---

## 3. Roles & Permissions

Every user has exactly one role, checked on every request:

| Role | Can do |
|---|---|
| **admin** | Everything an operator can, plus: manage users, view/change system settings, view host metrics, create/list DB backups, test & configure Cloudflare credentials |
| **operator** | Full app lifecycle (create, start/stop/restart, edit config, delete), file management, package management, log management, tunnel publish/unpublish |
| **viewer** | Read-only — list apps, view stats/logs/file trees, download files, view system info/ports. Cannot mutate anything |

Apps, files, and settings belong to the **Venvy instance**, not to the individual user who created them — any authenticated user of sufficient role can act on any app.

Two FastAPI dependencies enforce this on every route:
- `check_admin` → 403 unless `role == "admin"`
- `require_operator` → 403 unless `role in ("admin", "operator")`

Unauthenticated requests always receive `401`.

---

## 4. Conventions

### 4.1 Request/response format
All bodies are `application/json` except file uploads (`multipart/form-data`) and file downloads (raw binary via `Content-Disposition`).

### 4.2 Standard error shape
```json
{ "detail": "Human-readable error message." }
```
Validation errors (`422`) use FastAPI's standard shape:
```json
{
  "detail": [
    { "loc": ["body", "field"], "msg": "field required", "type": "missing" }
  ]
}
```

### 4.3 HTTP status codes used

| Code | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request — invalid input, invalid state transition |
| `401` | Not authenticated / session invalid or expired |
| `403` | Authenticated but forbidden (role, CSRF, IP allowlist, path traversal) |
| `404` | Resource not found |
| `409` | Conflict (e.g. duplicate backup filename) |
| `413` | Payload too large |
| `422` | Request validation error |
| `429` | Rate limited |
| `500` | Internal error |
| `502` | Upstream (e.g. Cloudflare API) failure |

### 4.4 Rate limiting

Two independent limiters, both keyed by client IP:

| Limiter | Scope | Limit |
|---|---|---|
| Login | `POST /api/auth/login` | 5 failed attempts → 5-minute lockout |
| Mutations | Every app/file/package/log/tunnel-mutating route except `POST /files/upload/chunk` (one large file is hundreds of chunks) | 60 requests / 60-second sliding window |

Both return `429` with an `X-Retry-After` header (seconds).

### 4.4a Background jobs

Operations that can run for minutes answer immediately with `{"job_id": "..."}` instead of blocking (Cloudflare cuts off proxied requests after 100 s): `POST /apps/upload`, `POST`/`DELETE /apps/{id}/packages`, `POST /system/backup`, `POST /system/backup/restore/{filename}`. Input validation still happens up front and fails the request itself with `4xx`.

#### `GET /api/jobs/{job_id}`
Any authenticated user; only the job's creator (or an admin) can see it. Finished jobs are kept for an hour.
```json
{ "id": "…", "kind": "package_install", "status": "running | done | error", "result": { }, "error": null, "code": null }
```
`result` is what the endpoint used to return directly; on `error`, `error` holds the message and `code` the HTTP status it corresponds to.

### 4.5 Path safety

All file-tree, file-op, download, and upload endpoints resolve the requested path against the app's sandbox root and reject anything that escapes it (`403`), as well as writes to files Venvy itself manages in the app root (`.jailrc*`, `.jailsh*`, `.bashrc-venvy*`).

---

## 5. Endpoint Reference

### 5.1 Auth — `/api/auth`

#### `GET /api/auth/status`
Unauthenticated. Reports instance/session state.
```json
{
  "user_exists": true,
  "authenticated": true,
  "username": "alice",
  "role": "admin",
  "totp_enabled": false
}
```

#### `POST /api/auth/setup`
One-time only — creates the first (admin) account and logs it in (response includes `auth_key`, see §2.1). `400` if any user already exists.

**Body:** `{ "username": "string", "password": "string", "role"?: "string" }` (role is ignored — always `admin`)
**Password policy:** ≥ 8 characters, must contain at least one letter and one digit.

#### `POST /api/auth/login`
**Body:** `{ "username": "string", "password": "string", "totp_code"?: "string" }`
**Responses:** `200` sets session cookie and returns `{ ok, username, role, auth_key }` · `401` invalid credentials/code · `403 "totp_required"` when TOTP is enabled and no code was sent · `429` when rate-limited.

#### `POST /api/auth/logout`
Deletes the server-side session record and clears the cookie. Idempotent.

#### `PUT /api/auth/password`
Change the caller's own password.
**Body:** `{ "current_password", "password", "password_confirm" }`
**Errors:** `401` wrong current password · `400` mismatch or weak new password.
On success every other session of this user is revoked; the calling session stays signed in.

#### `POST /api/auth/download-ticket`
Returns `{ "ticket": "..." }`: a one-time, 60-second ticket to append as `?ticket=` to `GET /files/download` or `GET /system/backups/{filename}/download` (see §2.1).

#### `GET /api/auth/totp/setup`
Generates (or re-returns) a pending TOTP secret + `otpauth://` URI for QR display. **Does not enable TOTP.**
```json
{ "ok": true, "secret": "BASE32SECRET", "uri": "otpauth://totp/Venvy:alice?..." }
```

#### `POST /api/auth/totp/setup/confirm`
**Body:** `{ "totp_code": "123456" }`
Verifies the code against the pending secret; on success, enables TOTP for the account. `400` if no pending setup or the code is wrong.

#### `POST /api/auth/totp/disable`
**Body:** `{ "password": "string" }`
Disables TOTP after re-verifying the current password.

---

### 5.2 User Management — `/api/users` (admin only)

#### `GET /api/users`
Returns `[{ "id", "username", "role" }, ...]`.

#### `POST /api/users`
**Body:** `{ "username", "password", "role"? }` — role defaults to `operator`; must be one of `admin | operator | viewer`.
**Errors:** `400` username taken, invalid role, or weak password.

#### `DELETE /api/users/{user_id}`
Deletes the account and its active sessions. **Does not** touch or reassign any apps (apps belong to the instance). `400` if attempting to delete yourself.

---

### 5.3 Apps — `/api/apps`

#### `GET /api/apps`
Authenticated (any role). Returns every app on the instance as an array of the **App object** (§6.1), reconciling stale state first (an app marked `running` whose PID has died is flipped to `stopped`). Secrets (env vars, decrypted values) are included only for `admin`/`operator` roles; `viewer` gets a redacted view — see §6.1.

#### `POST /api/apps/upload`
`require_operator`. `multipart/form-data`.

| Field | Type | Notes |
|---|---|---|
| `name` | string | required |
| `runtime` | string | default `python` (also supports `rust`) |
| `entrypoint` | string | default `""` |
| `start_on_boot` | bool | default `false` |
| `restart_policy` | string | `never` \| `on-failure` \| `always` |
| `files` | file[] | optional app source files (50 MB each). The web UI sends none here and uploads them afterwards via `POST /files/upload/chunk` |
| `requirements` | file | optional `requirements.txt` / `Cargo.toml` |

Creates the app's directory and DB record right away (status `installing`), then builds the virtual environment and installs `requirements.txt` in a **background job** (§4.4a; 10-minute pip timeout). Response: `{ "job_id", "app_id", "skipped_files": [{ path, reason }] }`. The job's `result` is the full **App object** (§6.1) with status `stopped`. If the job fails, the app, its directory and (with per-app users) its OS user are removed again. Starting or deleting an app while it is `installing` answers `409`.

#### `PUT /api/apps/{app_id}`
`require_operator`. Partial update — any field omitted (or `null`) is left unchanged.
```json
{
  "name": "string",
  "start_on_boot": true,
  "restart_policy": "on-failure",
  "runtime": "python",
  "port": 8001,
  "manual_ports": [8001, 8002],
  "env_vars": { "KEY": "value" },
  "entrypoint": "main.py",
  "url_source_pref": "local",
  "memory_limit_mb": 512,
  "cpu_limit_percent": 50
}
```

#### `DELETE /api/apps/{app_id}`
`require_operator`. Stops the process if running and removes the app's files, venv, and DB record.

#### `POST /api/apps/{app_id}/control?action={start|stop|restart}`
`require_operator`, rate-limited. Queues the lifecycle transition **asynchronously** and returns immediately with the intermediate status:
```json
{ "status": "starting", "queued": true }
```
Poll `GET /api/apps` (or `.../stats`) to observe the final `running` / `stopped` / `error` state. `400` for any action other than `start`, `stop`, or `restart`.

#### `GET /api/apps/{app_id}/stats`
Any authenticated role. Live process stats, sampled fresh on each call (not cached).
```json
{ "cpu": 3.2, "memory": 84.5, "uptime": 1523.7 }
```
`cpu` is percent (`psutil` instantaneous sample — expect noisy/`0.0` values unless you poll it more than once, since the first call after process start has no prior sample to diff against). `memory` is RSS in **MB**. `uptime` is seconds since `last_start_time`. If the app has no `pid` or `psutil` isn't installed, all three fields are `0`/`0.0` — this is not an error.

---

### 5.4 Files — `/api/apps/{app_id}/files`

All paths are relative to the app's sandbox root and validated to prevent traversal outside it.

#### `GET /files/tree?path=`
`require_operator`. Lists immediate children of `path` (default: app root). Dotfiles (e.g. `.env`) are listed; internal directories (`venv`, `__pycache__`, `logs`, `node_modules`, `.git`, etc.) and Venvy's own `.jailrc*`/`.jailsh*`/`.bashrc-venvy*` files are omitted. Sorted directories-first, then alphabetically. Viewers don't get file contents — app directories routinely hold `.env`/config files with real secrets, so this is gated the same as `files/op` rather than left on bare authentication.
```json
[{ "name": "main.py", "path": "main.py", "is_dir": false, "size": 1240 }]
```

#### `POST /files/op`
`require_operator`, rate-limited.
**Body:** `{ "operation", "path", "new_path"?, "content"? }`

| Operation | Required fields | Behavior |
|---|---|---|
| `create_file` | `path` | Creates parent dirs + an empty file |
| `create_folder` | `path` | Creates directory (and parents) |
| `delete` | `path` | Recursively deletes file or directory |
| `rename` | `path`, `new_path` | Moves the file/dir; `409` if `new_path` already exists |
| `copy` | `path`, `new_path` | Copies file (with metadata) or directory tree; `409` if `new_path` already exists |
| `save` | `path`, `content` | Overwrites file content (UTF-8) atomically (temp file + rename), keeping the file mode |

Writes to `.jailrc*` / `.jailsh*` / `.bashrc-venvy*` are rejected with `403` regardless of role. Every operation except `create_folder` refuses the app root itself (`path: ""`) with `400`, so e.g. `delete` can't wipe the whole app. With per-app users (§7), files Venvy creates are handed to the app's user.

#### `GET /files/download?path=`
`require_operator`. Streams the file as an attachment with cache-busting headers. `404` if the path isn't a regular file.

#### `POST /files/upload`
`require_operator`, rate-limited. `multipart/form-data`: `path` (target dir, default root) + `files[]`.
Single-request upload, kept for API clients (the web UI uses the chunked endpoint below). Files stream to disk, 50 MB per file.
- Each file's name may contain folders (`sub/dir/file.txt`). Rejected: absolute paths, `.`/`..`/empty segments, backslashes, control characters, segments over 255 bytes, and Venvy's reserved filenames.
- A rejected or oversized file doesn't fail the request. It is left out and listed: `{ "ok": true, "skipped": [{ "path", "reason" }] }`.

#### `POST /files/upload/chunk`
`require_operator`, **not** mutation-rate-limited. `multipart/form-data`, one chunk of one file per request:

| Field | Notes |
|---|---|
| `upload_id` | 32 lowercase hex chars, client-generated, one per file |
| `path` | target directory (default root) |
| `rel_path` | file path relative to `path`; same rules as `/files/upload` (`400` otherwise) |
| `offset` | byte offset of this chunk; must equal the bytes received so far |
| `total` | final file size in bytes |
| `chunk` | the bytes (≤ 16 MB) |

Chunks are appended to `DATA_DIR/.upload-tmp/`. Once `total` bytes are in, the file is moved into place atomically. Response: `{ "ok": true, "received": <bytes>, "done": <bool> }`.
- `409 {"detail": {"message", "received"}}`: offset mismatch (e.g. the previous response was lost). Resume from `received`.
- `413`: `total` is over the limit (`VENVY_MAX_UPLOAD_MB`, default 10240). `507`: not enough free disk space (checked on the first chunk).
- `409` with a string detail: the destination already exists as a folder.
- Partial files untouched for 24 h are purged automatically.

#### `DELETE /files/upload/{upload_id}`
`require_operator`. Discards a partial upload (e.g. after the user cancels).

---

### 5.5 Packages — `/api/apps/{app_id}/packages`

Supports Python apps (via the app's venv `pip`) and Rust apps (via `cargo`). Any other runtime returns `400`.

#### `GET /api/apps/{app_id}/packages`
Any role. Response shape **differs by runtime**:
- **Python:** raw output of `pip list --format=json` — `[{ "name": "requests", "version": "2.31.0" }, ...]`.
- **Rust:** parsed from `Cargo.toml`'s `[dependencies]` + `[dev-dependencies]` — `[{ "name": "serde", "version": "1.0" }, ...]` (version string as written in the manifest, e.g. `"1.0"` or `"unknown"` if it's a table without a `version` key).
- On any internal failure (missing venv, unparsable manifest, subprocess timeout) this endpoint returns `[]` rather than an error — a frontend should treat an empty array as "possibly failed, not necessarily zero packages" and not surface it as a hard error state.

#### `POST /api/apps/{app_id}/packages`
`require_operator`, rate-limited. **Body:** `{ "name": "package[extra]==1.2.3" }` (or `crate@version` for Rust).
Package specifiers are validated against a strict allow-list regex (and may not start with `-`, so they can't be read as a pip/cargo option) before being passed to `pip install` / `cargo add` (no shell is invoked). Runs as a **background job** (§4.4a); timeout 600 s (pip) / 300 s (cargo).

#### `DELETE /api/apps/{app_id}/packages/{name}`
`require_operator`, rate-limited. Uninstalls via `pip uninstall -y` / `cargo remove` as a **background job** (§4.4a). Timeout: 300 s (Python) / 120 s (Rust).

#### `GET /api/apps/{app_id}/scan-imports`
Any role. Python-only static analysis: walks the app's `.py` files via `ast`, collects top-level imports, and diffs them against stdlib + the venv's installed packages.
```json
{ "missing": ["requests", "numpy"], "installed_count": 24 }
```

---

### 5.6 Logs — `/api/apps/{app_id}/logs`

#### `GET /logs?lines=2000`
Any role. `lines` is clamped `1–10000` (default 2000). Reads at most the last 512 KB of the log file for performance, then returns the requested tail.
```json
{ "content": "...", "truncated": true, "total_lines": 5400 }
```

#### `DELETE /logs`
`require_operator`, rate-limited. Truncates the app's log file to empty.

Logs rotate while the app runs once `app.log` passes `VENVY_LOG_MAX_MB` (default 20); the newest 5 rotated files are kept. Lines longer than 64 KB are split. With per-app users (§7) logs live in `DATA_DIR/app-logs/<app_id>/` instead of the app directory.

---

### 5.7 System — `/api/system`

#### `GET /api/system/metrics` — admin only
Host-level CPU/memory/disk/load averages via `psutil` (returns `{"error": "psutil not installed"}` if the optional dependency is absent).

#### `POST /api/system/backup` — admin only
Creates a **full** backup: a consistent `data.db` snapshot (via `VACUUM INTO`, safe on a live WAL-mode database), the `.venvy_key` encryption key, and the entire `apps/` directory (code, venvs, logs), bundled into one `full-YYYYMMDD-HHMMSS.tar.gz` in `backups/`. This is the only kind of backup created from here on — one file is enough to fully restore Venvy, so backing up never requires shell/filesystem access to the server.
Runs as a **background job** (§4.4a); the job's `result` is:
```json
{ "ok": true, "path": "/.../backups/full-20260802-143000.tar.gz", "size_bytes": 104857600 }
```
Fails with `507` up front if free disk space looks insufficient. Only the newest `VENVY_BACKUP_KEEP` (default 10) full backups are kept; older ones are deleted after each new backup. `409` if a backup with the same second-resolution timestamp already exists (hard-linked into place atomically, so a losing concurrent request can never delete a winning one's file — see the source comment on `_create_full_backup_archive`). `500` on failure (disk full, tar error, etc).

#### `GET /api/system/backups` — admin only
Lists existing backups: `[{ "filename", "size_bytes", "created_at", "type" }, ...]`. `type` is `"full"` (the current format — restorable) or `"database"` (a legacy DB-only snapshot from an older Venvy version, kept listed so upgrading doesn't orphan it, but download/delete only — not restorable).

#### `GET /api/system/backups/{filename}/download` — admin only
Downloads a backup of either type. `filename` must match one of the fixed naming schemes (`full-YYYYMMDD-HHMMSS.tar.gz` or the legacy `data-YYYYMMDD-HHMMSS.db`); anything else is `400`, a non-existent file is `404`.

#### `DELETE /api/system/backups/{filename}` — admin only
Permanently deletes a backup of either type. Same filename validation as the download endpoint.

#### `POST /api/system/backup/restore/{filename}` — admin only
Rolls this **live** server back to a full backup. Only `type: "full"` backups are accepted (`400` for a legacy DB-only filename or anything not matching the naming scheme). **Body:** `{ "confirm": "RESTORE" }` — any other value is a `400` no-op, nothing is touched.

Sequence: (1) creates a fresh full backup of the *current* state first, so the restore itself can be undone if it turns out to be a mistake — if this safety backup fails, the request aborts here and nothing else is touched; (2) stops every running app; (3) extracts the target archive to a staging directory, rejecting any entry whose path would land outside it — including writes through symlinks extracted earlier (symlinks themselves are allowed: every venv has one to the system Python); (4) closes all database connections, removes the old `data.db-wal`/`-shm` (SQLite would otherwise replay them onto the restored database) and swaps `apps/`, `data.db`, and `.venvy_key` into place; (5) restarts the whole Venvy process (`os.execv` — same PID, fresh process image) so every piece of in-memory state (DB connections, the encryption-key cache, active-process tracking, background tasks) reinitializes cleanly against the restored files.

Steps 1–5 run as a **background job** (§4.4a); its `result` is `{ "ok": true, "restarting": true }`. The process restarts ~3 s after the job reports `done`, so a polling client sees the result before the connection drops. Apps that were running are stopped and stay stopped — start them again manually after reconnecting.

#### `GET /api/system/info`
Any role. Returns the server's LAN IPv4 address: `{ "ipv4": "192.168.1.20" }`.

#### `GET /api/system/ports`
`require_operator`. Lists currently listening TCP ports plus recommended free ports for new apps.

#### `GET /api/system/settings/{key}` / `PUT /api/system/settings/{key}` — admin only
Generic key-value store for instance settings. Keys prefixed `cf_` (Cloudflare credentials) and `gh_token` are transparently encrypted at rest with a local Fernet key (`.venvy_key`).
Secrets are write-only: `GET` for `cf_api_token` or `gh_token` answers `{ "key", "value": "", "is_set": true|false }` and never the stored value. Other keys (including `cf_account_id` / `cf_tunnel_id`) are returned decrypted.
**PUT body:** `{ "value": "string" }`

#### `POST /api/system/settings/batch` — admin only
**Body:** `[{ "key", "value" }, ...]` — saves multiple settings in one DB transaction. The same encryption rule applies per item.

#### `GET /api/system/cloudflare/status`
`require_operator`. Whether the Cloudflare integration is configured, without exposing the token: `{ "configured": true, "token_set": true, "account_id_hint": "0123abcd", "tunnel_id_hint": "89ef4567" }` (hints are the first 8 characters).

---

### 5.8 Cloudflare Tunnel — `/api/system/cloudflare`, `/api/apps/{app_id}/tunnel`

Lets an operator/admin expose a locally-running app on a public hostname through an existing Cloudflare Tunnel, updating both the tunnel's ingress rules and the zone's DNS.

#### `POST /api/system/cloudflare/test` — admin only
Verifies the saved `cf_api_token` / `cf_account_id` / `cf_tunnel_id` credentials and required permissions against the Cloudflare API.

#### `GET /api/system/cloudflare/zones` — `require_operator`
Lists active zones (domains) available on the configured Cloudflare account.

#### `GET /api/apps/{app_id}/tunnel`
Any role. Lists the app's current Cloudflare route mappings (hostname ↔ local port).

#### `GET /api/apps/{app_id}/tunnel/check?hostname=&local_port=`
`require_operator`. Pre-flight validation before publishing (hostname format, zone ownership, port availability).

#### `POST /api/apps/{app_id}/tunnel/publish`
`require_operator`, rate-limited. **Body:** `{ "hostname": "app.example.com", "local_port": 8001 }`
Updates the Cloudflare Tunnel's ingress config and upserts a DNS `CNAME` record; releases the hostname from any other app that previously held it. Sets the app's `external_url`.
```json
{ "ok": true, "hostname": "app.example.com", "url": "https://app.example.com", "dns_record_id": "..." }
```

#### `DELETE /api/apps/{app_id}/tunnel/publish?local_port=`
`require_operator`, rate-limited. Removes the ingress rule and the DNS `CNAME`. Clears `external_url` if no other route remains active for the app. `400` if no published route exists for that port.

---

### 5.9 Terminal — WebSocket

#### `WS /api/apps/{app_id}/terminal`
Checks, in order: the IP allowlist (§2.7); that the handshake's `Origin` matches the `Host` (or is in `ALLOWED_ORIGINS`) — WebSockets aren't covered by CORS, and pages on other ports of the same host (hosted apps) count as same-site; then authentication, from the `session` cookie plus a **first text frame** of `"\x01" + auth_key` (browsers can't set headers on a WebSocket handshake), which must arrive within 10 s. The shell inherits no file descriptors from Venvy beyond its PTY, and with per-app users (§7) it runs as the app's user. Subject to the IP allowlist (§2.7) — checked explicitly and first, since this route is registered directly on the app rather than on `api_router` and so doesn't pick up its `check_ip_allowlist` dependency automatically. Requires `operator` or `admin` role. Opens an interactive PTY (`bash --rcfile`, with the app's venv on `PATH` and its decrypted env vars injected) inside the app's sandboxed directory. Closes with code `1008` and a `reason` string if: IP not allowlisted, unauthenticated, session invalid/expired, user not found, app not found, or role is `viewer`.

**Wire protocol** — all frames are **text frames**, not binary:

| Direction | Frame content | Meaning |
|---|---|---|
| Server → client | Raw UTF-8 text | Terminal output (stdout/stderr of the PTY), sent as-is — the client should feed it straight into a terminal emulator (e.g. xterm.js) which handles ANSI escape codes itself. No framing/wrapping. |
| Client → server | Raw UTF-8 text | Keystrokes / stdin, written directly to the PTY. |
| Client → server | `"\x00" + JSON` | **Control message**, distinguished by a leading NUL byte. Currently only resize is supported: `\x00{"cols": 120, "rows": 40}`. Max control-frame size is 4096 bytes; malformed JSON is silently ignored. |

There is no message-type envelope for normal I/O — a frontend must special-case any outgoing frame that would start with `\x00` (it won't happen from real keystrokes) and use that solely for resize events, typically sent on terminal-emulator `onResize` and once immediately after connecting to establish the initial size (default PTY size is 220×50 if never sent). The server does not send an initial "connected" message — the socket opening (readyState `OPEN`) is the only success signal; a close event with code `1008` before that is the failure signal.

On disconnect, the server sends `SIGHUP` to the shell, waits up to ~3s, then `SIGKILL`s it — no explicit "process exited" message is sent to the client.

---

### 5.10 Unauthenticated / meta

#### `GET /api/health`
No auth, no IP allowlist, no CSRF — intended for uptime monitors and Cloudflare active health checks that must be reachable even when misconfigured. Performs a real database round-trip so a corrupted/wedged DB reports unhealthy rather than a false positive.
```json
{ "ok": true, "status": "healthy", "uptime_seconds": 41213 }
```
Returns `503` with `"status": "degraded"` if the DB probe fails.

#### `GET /`
Serves the single-page application shell (`index.html`).

---

## 6. Data Model Reference

### 6.1 App object

This is the exact shape returned by `GET /api/apps` (array of these), `POST /api/apps/upload`, and implicitly reflected by `PUT /api/apps/{id}`. It is the raw DB row **plus** four computed fields (`local_url`, `metrics`, `access_url`, `url_source`) added at serialization time — nothing is omitted for `admin`/`operator` roles.

```json
{
  "id": 3,
  "user_id": 1,
  "name": "my-flask-app",
  "slug": "my-flask-app",
  "runtime": "python",
  "created_at": "2026-07-01T12:00:00+00:00",
  "updated_at": "2026-07-01T12:00:00+00:00",
  "status": "running",
  "pid": 48213,
  "venv_path": "/path/to/apps/my-flask-app/venv",
  "app_dir": "/path/to/apps/my-flask-app",
  "entrypoint": "main.py",
  "start_on_boot": false,
  "restart_policy": "on-failure",
  "last_exit_code": 0,
  "last_start_time": "2026-08-01T09:00:00+00:00",
  "last_stop_time": null,
  "last_manually_stopped_at": null,
  "port": 8001,
  "manual_ports": "[8001]",
  "detected_ports": "[8001]",
  "external_url": "https://myapp.example.com",
  "env_json": "{\"API_KEY\":\"secret-value\"}",
  "healthcheck_path": null,
  "url_source_pref": "local",
  "memory_limit_mb": 512,
  "cpu_limit_percent": 50,
  "systemd_scope": "app-3.scope",
  "local_url": "http://192.168.1.20:8001",
  "metrics": { "cpu": 1.4, "memory": 62914560 },
  "access_url": "http://192.168.1.20:8001",
  "url_source": "local"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | primary key |
| `user_id` | int | **audit-only** — who created it. Not used for access control; any operator/admin can manage any app. |
| `name`, `slug` | string | |
| `runtime` | string | `python` \| `rust` |
| `created_at`, `updated_at` | string (ISO‑8601, UTC) | |
| `status` | string | `stopped` \| `starting` \| `running` \| `stopping` \| `error` |
| `pid` | int \| null | OS process id when running |
| `venv_path`, `app_dir`, `entrypoint` | string | filesystem locations |
| `start_on_boot` | bool | |
| `restart_policy` | string | `never` \| `on-failure` \| `always` |
| `last_exit_code` | int \| null | |
| `last_start_time`, `last_stop_time`, `last_manually_stopped_at` | string (ISO‑8601) \| null | |
| `port` | int \| null | primary port |
| `manual_ports`, `detected_ports` | **string** | ⚠️ JSON-encoded array **as a string**, e.g. `"[8001, 8002]"` — the client must `JSON.parse()` it, it is not a native JSON array. |
| `external_url` | string \| null | set when a Cloudflare route is published |
| `env_json` | **string** | ⚠️ also a JSON-encoded object **as a string** (`"{}"` if empty), must be `JSON.parse()`'d client-side. For `admin`/`operator` this is the **decrypted plaintext**; see the redaction note below. |
| `env_redacted` | bool | **only present when secrets are hidden** (i.e. caller is `viewer`) — `true` if the app has any env vars set, even though `env_json` itself will just be `"{}"`. Not present at all for `admin`/`operator` responses. |
| `healthcheck_path` | string \| null | |
| `url_source_pref` | string | `local` \| `public` — the operator's preference for which URL `access_url` should reflect |
| `memory_limit_mb` | int \| null | default `512`; `null` = unlimited |
| `cpu_limit_percent` | int \| null | default `50`; `null` = unlimited |
| `systemd_scope` | string \| null | set when launched under `systemd-run` |
| `resource_limits_enforced` | bool | *(computed)* `true` if this host has `systemd-run`/`systemctl` and systemd as PID 1 — i.e. whether `memory_limit_mb`/`cpu_limit_percent` are actually enforced rather than just stored. `false` on non-systemd hosts (macOS, most containers). |
| `local_url` | string \| null | *(computed)* `http://<server-lan-ip>:<port>`, or `null` if no port assigned |
| `metrics` | object *(computed)* | `{ "cpu": float, "memory": int }` — a cached last-seen sample from the background metrics collector (**not** a live read; use `GET /apps/{id}/stats` for a fresh one). Defaults to `{"cpu": 0.0, "memory": 0}` if never sampled. |
| `access_url` | string \| null | *(computed)* Resolves `url_source_pref`: `external_url` if pref is `public` and one exists, else `local_url`, else `null`. This is the URL a frontend should show as "open app". |
| `url_source` | string \| null | *(computed)* `"cloudflare"` \| `"local"` \| `null`, matching how `access_url` was derived. |

### User
`id`, `username` (unique), `password_hash` (scrypt), `role` (`admin`\|`operator`\|`viewer`), `totp_enabled`, `created_at`.

### CfRoute
Tracks each app's published Cloudflare hostname: `app_id`, `local_port`, `hostname`, `cf_route_id`.

---

## 7. Operational Notes

- **Resource isolation:** apps are launched under `systemd-run` when available, applying `memory_limit_mb` / `cpu_limit_percent` as hard caps.
- **Backups:** `POST /api/system/backup` produces one self-contained archive — `data.db`, the `apps/` directory, and `.venvy_key` — so both backup and restore go entirely through the API/UI now; no shell access to the server is needed for either. `POST /api/system/backup/restore/{filename}` restores a full backup onto a *live* server in place (stops all apps, swaps the files, restarts the process). The old manual "copy three artifacts into a fresh working directory" procedure documented in `main.py`'s top-of-file docstring still applies for the one case an API call fundamentally can't cover: standing Venvy back up on a *new* or *replacement* host where no Venvy instance is running yet to call the API on.
- **Per-app users (opt-in):** set `VENVY_APP_USERS=1` and run Venvy as root on Linux. Each app then gets a locked system user `venvy-app-<id>` that owns only its own directory (`0700`; `apps/` and `apps/<uid>/` are `0711`). The app's process, its terminal, and every venv/pip/cargo run for it execute as that user, so an app, or a terminal opened on it, can no longer read `data.db`, `.venvy_key` or other apps. Without it, everything runs as Venvy's own OS user and "operator" access is effectively full access to the host account. Rust apps need `cargo` installed system-wide (e.g. `apt install cargo`), not in root's home.
- **Data files:** `data.db`, `.venvy_key` and backups are created `0600`/`0700`.
- **Crash loops:** automatic restarts back off 5 s → 10 s → … → 5 min, and reset after an app has stayed up for 60 s. A manual start resets it immediately.
- **PID reuse:** Venvy records each app process's start time next to its PID and never signals a process whose start time differs (e.g. after a reboot the PID now belongs to something else).
- **Password hashing:** scrypt (`n=16384, r=8, p=1`); legacy PBKDF2-HMAC-SHA256 hashes are still verifiable for backward compatibility.
- **Security headers:** every response includes `X-Content-Type-Options`, `X-Frame-Options: DENY`, a restrictive `Content-Security-Policy`, and related hardening headers.

---

## 7a. Installing and Updating

On a fresh Debian 12+ / Ubuntu 22.04+ machine:

```
curl -fsSL https://github.com/nikolaus018/venvy/releases/latest/download/install.sh | sudo bash
```

Options follow `bash -s --`, e.g. `… | sudo bash -s -- --host 127.0.0.1 --port 8080`
(`--version TAG`, `--with-rust`, `--app-users`, `--uninstall`). The installer adds
missing system packages, creates a `venvy` system user, puts the code in
`/opt/venvy/releases/<version>/` (with its own Python venv), data in `/var/lib/venvy/`,
settings in `/etc/venvy/venvy.env`, and a `venvy` systemd service that starts at boot.

Running the same command again updates: the release is downloaded, checksum-verified and
prepared beside the running one, the database is snapshotted into `backups/`, the service is
switched over, and it is switched back automatically if `/api/health` does not answer.
An installation that predates the installer keeps its data directory where it is.

Releases are built by `.github/workflows/release.yml` when a `v*` tag is pushed.

## 8. Source Layout

```
main.py                 entry point: dependency check, banner, `python main.py --host … --port …`
venvy/                  the backend package (see venvy/__init__.py for one line per module)
  config, crypto, models, db, auth, safefs, isolation, jobs, process, apps,
  terminal, backup, cloudflare, settings, github, uploads, startup, webui, app
  api/                  one APIRouter per resource: auth, users, apps, files, system,
                        cloudflare, github, terminal
web/
  index.html            page skeleton; `<!-- include: partials/x.html -->` lines are expanded
                        on the server (venvy/webui.py), so the browser gets one document
  partials/             markup per view: sidebar, dashboard, tabs/*, system-view, modals/*
  static/css/app.css    styles
  static/js/*.js        one script per feature, plus api.js (API client + shared state);
                        classic scripts sharing one global scope, loaded in the order
                        index.html lists them — there is no build step
  sw.js                 service worker (served at /sw.js)
vendor/                 third-party scripts shipped with Venvy (served at /vendor)
emulator.py             fake backend for UI development; serves the same web/ tree
tests/                  end-to-end suite: `tests/run_all.sh`
install.sh              installer / updater (see "Installing and Updating")
requirements.txt        Venvy's own Python dependencies, pinned
.github/workflows/      release.yml builds and publishes a release on a v* tag
```

A frozen build must bundle `web/` and `vendor/` next to the executable's code.

## 9. Quick Reference Table

| Method | Path | Min. role |
|---|---|---|
| GET | `/api/health` | none |
| GET | `/api/auth/status` | none |
| POST | `/api/auth/setup` | none (first run only) |
| POST | `/api/auth/login` | none |
| POST | `/api/auth/logout` | authenticated |
| PUT | `/api/auth/password` | authenticated |
| GET | `/api/auth/totp/setup` | authenticated |
| POST | `/api/auth/totp/setup/confirm` | authenticated |
| POST | `/api/auth/totp/disable` | authenticated |
| GET | `/api/users` | admin |
| POST | `/api/users` | admin |
| DELETE | `/api/users/{id}` | admin |
| GET | `/api/apps` | viewer |
| POST | `/api/apps/upload` | operator |
| PUT | `/api/apps/{id}` | operator |
| DELETE | `/api/apps/{id}` | operator |
| POST | `/api/apps/{id}/control` | operator |
| GET | `/api/apps/{id}/stats` | viewer |
| GET | `/api/apps/{id}/files/tree` | operator |
| POST | `/api/apps/{id}/files/op` | operator |
| GET | `/api/apps/{id}/files/download` | operator |
| POST | `/api/apps/{id}/files/upload` | operator |
| POST | `/api/apps/{id}/files/upload/chunk` | operator |
| GET | `/api/jobs/{job_id}` | any (own jobs) |
| POST | `/api/auth/download-ticket` | any |
| DELETE | `/api/apps/{id}/files/upload/{upload_id}` | operator |
| GET | `/api/apps/{id}/packages` | viewer |
| POST | `/api/apps/{id}/packages` | operator |
| DELETE | `/api/apps/{id}/packages/{name}` | operator |
| GET | `/api/apps/{id}/logs` | viewer |
| DELETE | `/api/apps/{id}/logs` | operator |
| GET | `/api/apps/{id}/scan-imports` | viewer |
| GET | `/api/system/info` | viewer |
| GET | `/api/system/ports` | viewer |
| GET | `/api/system/metrics` | admin |
| POST | `/api/system/backup` | admin |
| GET | `/api/system/backups` | admin |
| GET | `/api/system/backups/{filename}/download` | admin |
| DELETE | `/api/system/backups/{filename}` | admin |
| POST | `/api/system/backup/restore/{filename}` | admin |
| GET/PUT | `/api/system/settings/{key}` | admin |
| POST | `/api/system/settings/batch` | admin |
| GET | `/api/system/cloudflare/status` | operator |
| POST | `/api/system/cloudflare/test` | admin |
| GET | `/api/system/cloudflare/zones` | operator |
| GET | `/api/apps/{id}/tunnel` | viewer |
| GET | `/api/apps/{id}/tunnel/check` | operator |
| POST | `/api/apps/{id}/tunnel/publish` | operator |
| DELETE | `/api/apps/{id}/tunnel/publish` | operator |
| WS | `/api/apps/{id}/terminal` | authenticated |
