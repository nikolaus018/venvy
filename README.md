<h1 align="center">Venvy</h1>

<p align="center">
  <b>A self-hosted control panel for running your Python and Rust apps on one server.</b><br>
  Upload code, get a virtualenv, a process supervisor, a browser terminal, live logs and a public URL.
</p>

<p align="center">
  <img src="docs/screenshots/terminal.png" alt="Venvy dashboard with the in-browser terminal" width="900">
</p>

## Install

On a fresh Debian 12+ or Ubuntu 22.04+ machine:

```bash
curl -fsSL https://github.com/nikolaus018/venvy/releases/latest/download/install.sh | sudo bash
```

That installs the system packages it needs, creates a `venvy` service user, sets up a systemd
service that starts at boot, and prints the URL. The first visit creates the admin account.

Run the same command again to update. The new version is downloaded, checksum-verified and
prepared next to the running one; the database is snapshotted; and if the new version does not
come up healthy, the installer switches back on its own.

Options go after `bash -s --`:

```bash
curl -fsSL https://github.com/nikolaus018/venvy/releases/latest/download/install.sh \
  | sudo bash -s -- --host 127.0.0.1 --port 8080
```

| Option | Meaning |
|---|---|
| `--host ADDR` | Address to bind. Default `0.0.0.0`; use `127.0.0.1` to be reachable only through a Cloudflare tunnel. |
| `--port N` | Port. Default `54783`. |
| `--version TAG` | Install a specific release instead of the latest. |
| `--with-rust` | Also install `cargo`/`rustc` so Rust apps can be hosted. |
| `--app-users` | Run as root with one Unix user per hosted app (see [Isolation](#isolation)). |
| `--uninstall` | Remove the service and code. Data and config are kept. |

Settings live in `/etc/venvy/venvy.env`, data in `/var/lib/venvy`, code in `/opt/venvy`.

## What it does

- **Apps as first-class things.** Create an app, upload a folder (chunked, resumable), and Venvy
  builds its virtualenv and installs `requirements.txt`. Start, stop and restart from the UI.
- **Supervision.** Restart policies (`never`, `on-failure`, `always`) with crash-loop back-off,
  start-on-boot, port detection, and per-app CPU and memory limits through systemd scopes.
- **A real terminal in the browser.** A PTY shell in the app's directory with its virtualenv
  active, including a key bar for phones.
- **Files.** Tree, editor (Ace), upload, download, rename, copy, drag and drop.
- **Logs.** Live tail with ANSI colours, filtering, follow and wrap, rotation on the server.
- **Packages.** List, install and remove; a scanner that finds imports missing from the venv.
- **Git.** Connect an app to a GitHub repo (device-flow sign-in), then fetch, pull, commit, push.
- **Public URLs.** Publish an app on your own domain through a Cloudflare Tunnel; Venvy manages
  the tunnel ingress rule and the DNS record.
- **Backups.** One archive holds the database, the encryption key and every app. Restore rolls
  the live server back in place, after taking a safety backup first.
- **Users and roles.** Admin, operator and read-only viewer, with optional TOTP two-factor.
- **Installable PWA** with light and dark themes, a command palette (<kbd>Ctrl</kbd>+<kbd>K</kbd>)
  and keyboard shortcuts.

<table>
  <tr>
    <td><img src="docs/screenshots/files.png" alt="File manager and editor"></td>
    <td><img src="docs/screenshots/logs.png" alt="Live logs"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/system.png" alt="System view: host metrics, ports, backups"></td>
    <td><img src="docs/screenshots/settings.png" alt="App settings"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/palette.png" alt="Command palette"></td>
    <td><img src="docs/screenshots/light.png" alt="Light theme"></td>
  </tr>
</table>

<p align="center">
  <img src="docs/screenshots/mobile-terminal.png" alt="Terminal on a phone" width="240">
  &nbsp;
  <img src="docs/screenshots/mobile-logs.png" alt="Logs on a phone" width="240">
  &nbsp;
  <img src="docs/screenshots/mobile-sidebar.png" alt="App list on a phone" width="240">
</p>

## How it is built

A FastAPI backend with SQLite, and a framework-free frontend. No build step anywhere: the
server runs from source, and the browser loads plain scripts.

```
main.py            entry point
venvy/             backend package
  config  crypto  models  db  auth       settings, encryption, tables, sessions and roles
  safefs  isolation                      symlink-safe file access, per-app Unix users
  process  terminal  backup              lifecycle and supervisor, PTY terminal, backup/restore
  cloudflare  github  settings  jobs     integrations, setting rules, background jobs
  api/                                   one router per resource
  app                                    FastAPI app: lifespan, middleware, static routes
web/
  index.html  partials/                  page skeleton and per-view markup, assembled server-side
  static/css  static/js                  styles; one script per feature
tests/             end-to-end suite
install.sh         installer and updater
```

A few decisions worth pointing out:

- **Two secrets per session.** Browsers send cookies to every port on a host, including the
  hosted apps. So the session cookie alone is not enough: each request also carries a
  per-session key from `localStorage`, which is scoped to Venvy's own origin.
- **Secrets encrypted at rest.** App environment variables, the Cloudflare and GitHub tokens
  and TOTP secrets are Fernet-encrypted with a key that never leaves the host and is never
  returned by the API.
- **No path is trusted twice.** File operations resolve a request path once, then walk it from
  the app directory one component at a time with `O_NOFOLLOW` and work relative to the
  resulting directory descriptor, so a directory swapped for a symlink mid-request is refused
  rather than followed.
- **One lock per app.** Start, stop and restart are serialised per app, and the supervisor
  re-reads state under that lock, so a double click or a restart racing a crash cannot leave
  an untracked process behind.
- **Restore without swapping files under a live database.** The backup's snapshot is loaded
  into the running SQLite file through its backup API, then the process re-executes itself.

The API is documented in [venvy-api-documentation.md](venvy-api-documentation.md).

### Isolation

By default every hosted app runs as the same `venvy` user. That keeps apps away from the rest
of the system, but not from each other or from Venvy's own data. Installing with `--app-users`
runs Venvy as root and gives each app its own locked Unix user that owns only its directory;
its process, terminal and package installs run as that user.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python main.py                 # serves http://127.0.0.1:54783, data in the current directory
```

To work on the UI without a real backend, `python emulator.py` serves the same `web/` tree
against fake data.

### Tests

The suite boots a real server on a throwaway data directory and drives it over HTTP and
WebSocket: login and roles, files and uploads, the process lifecycle under concurrency,
backup and in-place restore, the terminal, and a Chrome pass over every view.

```bash
pip install -r tests/requirements.txt
tests/run_all.sh
```

### Releases

Pushing a `v*` tag runs the smoke test and publishes `venvy.tar.gz`, its SHA-256 and
`install.sh` as a GitHub release, which is what the install command downloads.

## Status

Venvy is a personal project that I run on my own servers. The test suite runs on macOS and,
for releases, on Linux in CI. Two things have less coverage than the rest and are worth knowing
before you depend on them: per-app-user mode (`--app-users`) and resource limits via systemd
scopes both need root on a real Linux host, which the automated suite does not exercise.
