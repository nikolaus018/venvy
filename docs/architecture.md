# Architecture

How Venvy is put together, and why it is put together that way.

## The shape of the problem

Venvy is a control plane for one machine. It has to do four things at once:

1. serve an HTTP API and a UI,
2. supervise long-lived child processes it did not write,
3. hold durable state that must survive a crash,
4. talk to an external API (Cloudflare) that can fail halfway.

Each of those wants something different from the code, and most of the design
decisions below are about keeping them from contaminating each other.

## Layers

```
api/         HTTP surface — routing, validation, status codes
   │         thin: no business logic lives here
   ▼
services/    what Venvy actually does — start a process, publish a hostname
   │
   ▼
db/          persistence: SQLModel over SQLite
   │
   ▼
core/        pure helpers: crypto, TOTP, path safety, port discovery
```

The dependency arrow only points down. In particular **nothing in `core/`
imports FastAPI, SQLModel, or any other Venvy package.** That is what makes it
importable and unit-testable anywhere, and it is why the password, TOTP and
path-containment logic can be tested without standing up an application.

`security/` sits beside these: it holds the FastAPI dependencies (`check_admin`,
`require_operator`), the rate limiters, and the middleware.

## Request lifecycle

```
request
  │
  ├─ CSRFMiddleware            state-changing /api/ requests must echo the cookie
  ├─ SecurityHeadersMiddleware CSP, frame options, referrer policy
  ├─ CORSMiddleware            same-origin by default
  │
  ├─ check_ip_allowlist        router-level dependency
  ├─ get_current_user          resolves and slides the session
  ├─ require_operator          role check
  ├─ rate_limit_mutations      per-IP ceiling on writes
  │
  └─ handler → service → db
```

Starlette runs middleware in reverse registration order, so CSRF — added last —
is outermost and rejects a forged request before anything else does work for it.

## Why the state machine is the way it is

An app's `status` has five values, and two of them are unusual:

| | |
|---|---|
| `stopped`, `running`, `error` | settled states |
| `starting`, `stopping` | transient markers set by the API |

Starting an app can take minutes — a virtualenv to build, a Rust crate to
compile. Holding an HTTP request open for that is a bad experience and a worse
timeout story. So `POST /control` writes the transient state, schedules the real
work with `asyncio.create_task`, and returns immediately; the UI polls.

The consequence is that **the supervisor must ignore transient states.** If it
did not, it would race the in-flight request that set them. That check is at the
top of the supervisor loop and in `list_apps`.

This is also why `control_app` must stay `async`: a synchronous FastAPI handler
runs in a thread pool with no running event loop, and `asyncio.create_task`
would raise. It is annotated as such in the code.

## Two sources of truth, deliberately

The database records what *should* be running.
`services/registry.py` records what *this process* actually launched.

They can disagree, and the disagreement is informative:

- an app dies between polls → database says `running`, the PID is gone,
- Venvy is killed with `-9` → the database keeps stale `running` rows,
- an app is started, Venvy restarts, the app is still alive → the registry is
  empty but the PID is real.

Startup reconciles the two (`lifespan._reconcile_stale_state`), and shutdown
draws the kill list from **both**, because either alone would miss processes.

## Restart policy

Three rules, in order of precedence:

1. **A manual stop wins.** `last_manually_stopped_at >= last_start_time` means
   an operator took the app down; `always` must not undo that.
2. **Only apps started this session are resurrected**, unless they are marked
   start-on-boot. Otherwise restarting Venvy would bring back everything the
   operator had deliberately left down.
3. **Then the policy applies** — `always` restarts unconditionally,
   `on-failure` only on a non-zero exit.

There is a five-second floor between a stop and a policy-driven restart, so a
permanently-crashing app is a slow loop rather than a busy one.

## Process teardown

Killing a managed app is harder than it looks, and the code reflects that:

- Apps launch with `start_new_session=True`, so each gets its own process group
  and signalling it can never reach Venvy.
- The **full descendant tree is snapshotted before signalling.** A wrapper — a
  shell script, `npm`, a dev-server launcher — often dies on SIGTERM before its
  child does. Waiting only on the leader would declare victory while a
  grandchild still held the port.
- Group signalling is checked against Venvy's own PGID first. Getting this
  wrong means the server kills itself.
- SIGTERM, wait, then SIGKILL. Both stages confirm death by polling.
- When systemd is available, `systemctl stop` runs *as well*, not instead:
  `systemd-run --scope` execs in place, so the scope shares the PID and the
  signal pass is a harmless no-op when systemd already did the work — and the
  safety net when it could not.

## The Cloudflare two-phase problem

Publishing a hostname touches two independent Cloudflare resources:

1. an ingress rule in the tunnel's configuration,
2. a proxied CNAME in the zone's DNS.

There is no transaction across them. If the DNS half fails after the ingress
half succeeded, the tunnel routes a hostname that resolves nowhere — which
looks, to a user, exactly like Venvy being broken.

So `api/cloudflare.py` rolls the ingress change back by hand on every failure
path: zone lookup failure, zone not found, and DNS upsert failure. The rollback
is best-effort and logs loudly if it fails, because at that point there is
nothing further to try automatically.

## Secrets at rest

App environment variables and Cloudflare credentials are Fernet-encrypted with
a key in `.venvy_key`. Two properties matter:

- **Values written before encryption existed still work.** `decrypt_secret`
  recognises the absence of the `enc:v1:` marker and returns the value
  unchanged, so an upgrade does not lose anything.
- **A lost key fails closed.** Decryption failure returns an empty string and
  logs an error naming the cause, rather than raising into a request handler.

This is why the key must be backed up *with* the database: neither is useful
alone.

## The frontend

Native ES modules, served directly, no bundler.

```
web/
├── index.html      markup only
├── styles/         tokens → base → components → terminal → animations → layout
└── js/
    ├── main.js     entry point + the window bridge
    ├── core/       api (CSRF fetch wrapper), state, dom, pwa
    ├── ui/         sidebar, swipe, tabs, modals, contextmenu
    └── features/   auth, apps, files, terminal, logs, packages,
                    settings, cloudflare, users, system, createapp
```

**Shared state is one object.** `core/state.js` exports a mutable `state`
object rather than individual bindings, because ES module exports are read-only
for importers — `export let activeAppId` could be observed from another module
but never assigned. State used by a single feature stayed in that feature's
module; only what genuinely crosses boundaries is in `state`.

**The window bridge is deliberate.** `index.html` uses inline `onclick`
attributes, which resolve against the global scope, and module scope is not
global. `main.js` therefore republishes the handler functions onto `window`.
This is the one place that touches globals, and `tools/check-modules.mjs`
verifies in CI that every inline handler actually reaches it.

The trade-off: no build step means deployment is "copy a directory" and the
shipped code is the code you read. The cost is that nothing catches a broken
import at build time — hence the module-graph checker, which parses every file,
resolves every import against real exports, and flags any identifier that is
referenced but neither declared nor imported.

## Optional dependencies

`psutil` is optional. Every place that uses it has a `/proc` fallback, and
`core/process_compat.py` is the single place that answers "is it available?".
CI runs the suite once without it to make sure the fallbacks stay honest.

The same pattern covers POSIX features: `pty`, `fcntl` and `termios` are
imported behind a guard in `services/terminal.py`, and process-group calls
behind `HAS_PROCESS_GROUPS`. Venvy still requires Linux to be *useful*, but it
*imports* anywhere, which is what lets the whole test suite run on any
developer machine.
