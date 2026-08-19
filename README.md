<div align="center">

# Venvy

**A self-hosted application manager for a single Linux box.**

Upload an app, get an isolated environment, a supervised process, live logs,
a file editor, a jailed shell, and a public HTTPS URL — from a browser.

[![CI](https://github.com/nikolaus018/venvy/actions/workflows/ci.yml/badge.svg)](https://github.com/nikolaus018/venvy/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-checked-2a6db2)](https://mypy-lang.org/)

</div>

---

## What it is

Venvy is the small piece between "I have a Python script" and "it is running
on my server, restarts when it crashes, and has a URL". It is a single process
that manages other processes on one host.

It is **not** a Kubernetes, a Heroku, or a multi-tenant platform. It is a
control panel for a machine you already own, aimed at a homelab, a side
project, or a small team's internal tools.

| | |
|---|---|
| **Isolated environments** | Every Python app gets its own virtualenv; Rust apps get their own cargo build. |
| **Process supervision** | Crashed apps are noticed and restarted per policy — `never`, `on-failure`, or `always`. |
| **Resource limits** | Per-app CPU and memory ceilings, enforced through transient systemd scopes. |
| **Live logs** | Tailed in the browser, with ANSI colour, rotation, and a size cap. |
| **File editor** | Ace-backed browsing and editing, contained to the app's own directory. |
| **Jailed terminal** | A real PTY with the app's virtualenv activated, pinned to its directory. |
| **Package management** | pip and cargo from the UI, plus a scan for imports you have not installed. |
| **Public URLs** | Publish an app to `app.example.com` over a Cloudflare Tunnel — ingress *and* DNS handled for you. |
| **Accounts and roles** | `admin` / `operator` / `viewer`, with TOTP two-factor auth. |
| **Secrets at rest** | App environment variables and Cloudflare credentials are encrypted in the database. |

---

## Quick start

```bash
pip install venvy
```

```bash
mkdir -p /srv/venvy && cd /srv/venvy && venvy
```

Open <http://127.0.0.1:54783> and create the first admin account. That account
is created once; the endpoint refuses to run again afterwards.

Everything Venvy owns lives in the directory you launched it from — the
database, the apps, the backups, and the encryption key.

### Docker

```bash
docker compose up -d
```

Add the Cloudflare Tunnel sidecar with `--profile tunnel` once
`CLOUDFLARE_TUNNEL_TOKEN` is set in `.env`.

### From source

```bash
git clone https://github.com/nikolaus018/venvy.git
cd venvy
pip install -e ".[dev,metrics]"
pytest
venvy --host 127.0.0.1 --port 54783
```

---

## Requirements

| | |
|---|---|
| **OS** | Linux. Process groups, PTYs and `/proc` are all load-bearing. |
| **Python** | 3.11 or newer. |
| **`python3-venv`** | Required to create Python apps. Venvy probes for it at startup and tells you the exact command if it is missing. |
| **`cargo`** | Only for Rust apps. |
| **systemd** | Optional. Without it apps still run, just without enforced CPU/memory limits. |
| **`psutil`** | Optional (`pip install "venvy[metrics]"`). Without it the resource graphs go dark; everything else uses `/proc` instead. |

---

## Architecture

```
                    browser (ES modules, no build step)
                              │
                    ┌─────────┴─────────┐
                    │   FastAPI  app    │   CSRF · security headers · IP allowlist
                    └─────────┬─────────┘
          ┌───────────────────┼───────────────────┐
          │                   │                   │
     ┌────┴─────┐      ┌──────┴──────┐     ┌──────┴──────┐
     │ api/     │      │ services/   │     │ security/   │
     │ routers  │─────▶│ processes   │     │ auth, roles │
     │          │      │ supervisor  │     │ rate limits │
     │          │      │ terminal    │     └─────────────┘
     │          │      │ cloudflare  │
     └────┬─────┘      │ metrics     │
          │            └──────┬──────┘
          │                   │
     ┌────┴───────────────────┴────┐
     │  db/  SQLModel over SQLite  │      core/  crypto · TOTP · paths · net
     └─────────────────────────────┘
```

```
src/venvy/
├── config.py           environment-driven settings, read in one place
├── app.py              the ASGI application factory
├── lifespan.py         startup reconciliation and shutdown teardown
├── cli.py              the `venvy` command
├── core/               dependency-free helpers, importable anywhere
│   ├── crypto.py       secrets at rest (Fernet)
│   ├── passwords.py    scrypt hashing, with legacy PBKDF2 verification
│   ├── totp.py         RFC 6238, standard library only
│   ├── paths.py        path containment — the one place it is decided
│   ├── net.py          port discovery, via psutil or /proc
│   └── process_compat.py   psutil and POSIX feature detection
├── db/                 models, engine, forward-only migrations
├── security/           auth dependencies, rate limiting, CSRF, headers
├── services/           process lifecycle, supervision, PTY, Cloudflare
├── api/                one router per resource
└── web/                the frontend
    ├── index.html      markup only
    ├── styles/         six stylesheets, tokens first
    └── js/             24 ES modules + a generated entry point
```

### Two decisions worth knowing about

**The frontend has no build step.** It is native ES modules, served directly.
That keeps deployment to "copy a directory" and makes the shipped code the same
code you read. The cost is a `window` bridge in `main.js`, because
`index.html`'s inline `onclick` attributes resolve against the global scope;
`tools/check-modules.mjs` verifies in CI that every handler actually gets there.

**The terminal jail is a guardrail, not a sandbox.** The shell runs as the same
user as Venvy, so an operator can reach the rest of the host through it. Treat
operator access as equivalent to shell access, and see
[docs/security.md](docs/security.md).

---

## Configuration

Everything is environment variables; there is no config file.

| Variable | Default | Meaning |
|---|---|---|
| `VENVY_DATA_DIR` | working directory | Database, apps, backups and encryption key. |
| `VENVY_HOST` / `VENVY_PORT` | `127.0.0.1` / `54783` | Bind address. |
| `VENVY_LOG_LEVEL` | `info` | Logging verbosity. |
| `SESSION_COOKIE_SECURE` | `0` | Set to `1` when serving over HTTPS. |
| `IP_ALLOWLIST` | *(empty)* | Comma-separated allowlist. Empty means no restriction. |
| `ALLOWED_ORIGINS` | *(empty)* | CORS origins. Empty means same-origin only. |
| `VENVY_SESSION_TTL` | `2592000` | Session lifetime in seconds (30 days). |
| `VENVY_LOGIN_MAX_ATTEMPTS` | `5` | Failed sign-ins before lockout. |
| `VENVY_LOGIN_LOCKOUT` | `300` | Lockout duration in seconds. |
| `VENVY_MAX_UPLOAD_BYTES` | `52428800` | Per-file upload cap (50 MB). |
| `VENVY_USE_SYSTEMD` | `1` | Launch apps in transient scopes to enforce limits. |
| `VENVY_AUTO_INSTALL_SYSTEM_DEPS` | `0` | Allow `apt-get install` at startup for missing toolchains. |

---

## Roles

| Capability | viewer | operator | admin |
|---|:--:|:--:|:--:|
| See apps, logs, metrics | ● | ● | ● |
| Read app environment variables | | ● | ● |
| Start / stop / restart | | ● | ● |
| Edit files, manage packages | | ● | ● |
| Open a terminal | | ● | ● |
| Publish a public hostname | | ● | ● |
| Manage users | | | ● |
| System settings and backups | | | ● |

Apps belong to the instance, not to whoever uploaded them: every signed-in user
sees every app, and deleting a user never deletes an app.

---

## Backups

Two things must be backed up **together**:

1. `data.db` — take a consistent snapshot with `POST /api/system/backup`
   (`VACUUM INTO`). Do not `cp` the live file; it is in WAL mode.
2. `.venvy_key` — without it the encrypted columns in a restored database are
   unrecoverable. Treat it like a private key.

`apps/` can be copied at any time with `rsync -a`.

Full procedure in [docs/operations.md](docs/operations.md).

---

## Development

```bash
pip install -e ".[dev,metrics]"
npm install                # frontend linting only
pre-commit install
```

| | |
|---|---|
| `pytest` | 228 tests |
| `ruff check src tests tools` | lint |
| `ruff format src tests tools` | format |
| `mypy` | type-check |
| `npm run lint` | frontend lint |
| `npm run check:modules` | verify the frontend module graph |

`tools/dev_emulator.py` serves the frontend against fabricated data, so UI work
needs no Linux host and no real apps.

Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).
Security reports: [SECURITY.md](SECURITY.md).

---

## Documentation

- [docs/api.md](docs/api.md) — the HTTP API. Interactive docs live at `/api/docs`.
- [docs/architecture.md](docs/architecture.md) — how the pieces fit and why.
- [docs/security.md](docs/security.md) — threat model and hardening.
- [docs/operations.md](docs/operations.md) — deployment, backup, upgrade.

---

## License

MIT — see [LICENSE](LICENSE).
