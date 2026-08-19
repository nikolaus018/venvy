# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.0.0]

A structural release. The application's behaviour is essentially unchanged; how
it is organised, tested and shipped is not.

Before: three files at the repository root — a 4,300-line `main.py`, a
386 KB `index.html` with its CSS and 3,900 lines of JavaScript inline, and a
development emulator. After: an installable package with 45 backend modules,
24 frontend modules, 228 tests, and CI.

### Added

- **Packaging.** `pyproject.toml` with a `venvy` console script, a
  `python -m venvy` entry point, and the frontend shipped inside the wheel.
  CI installs the built wheel into a clean environment and asserts the UI is
  actually in it.
- **Test suite.** 228 tests covering password hashing (including an RFC 6238
  TOTP test vector), path containment, the full role matrix endpoint by
  endpoint, CSRF, login lockout, secrets-at-rest, the Cloudflare ingress
  helpers, and the supervisor's restart policy.
- **CI.** Lint, format, type-check, tests on Python 3.11/3.12/3.13, a run
  without `psutil` to exercise the `/proc` fallbacks, frontend lint, module
  graph verification, and a distribution build.
- **Tooling.** `ruff`, `mypy`, `pre-commit`, ESLint, `.editorconfig`, a
  Dockerfile and a Compose file with an optional `cloudflared` sidecar.
- **`tools/check-modules.mjs`.** Verifies the frontend module graph — that
  every file parses, every import resolves to a real export, and every inline
  `onclick` in `index.html` reaches `window`. Without a bundler nothing else
  would catch a broken import before the page came up blank.
- **Documentation.** README, `CONTRIBUTING.md`, `SECURITY.md` with a stated
  threat model and known limitations, and `docs/`.
- **Configuration.** Every setting now comes from one place
  (`venvy.config.Settings`); no other module reads `os.environ`. New knobs:
  `VENVY_DATA_DIR`, `VENVY_USE_SYSTEMD`, `VENVY_AUTO_INSTALL_SYSTEM_DEPS`,
  and the rate-limit and log-rotation parameters.

### Changed

- **Backend split** into `core/` (dependency-free helpers), `db/`,
  `security/`, `services/` and `api/`, with a one-way dependency direction.
  Route handlers are thin and delegate to services.
- **Frontend split** into six stylesheets and 24 ES modules with real import
  boundaries, loaded natively — still no build step. Shared state moved into
  a single `state` object; state used by one feature stayed with that feature.
- **The package now imports on any platform.** `pty`, `fcntl` and `termios`
  moved behind a guard in `services/terminal.py`, and process-group operations
  behind feature detection. Terminals still require POSIX and say so clearly;
  everything else — and the whole test suite — runs anywhere.
- **The database engine is created lazily** instead of at import time, so
  importing Venvy no longer creates a `data.db` as a side effect.
- **Installing system packages at startup is now opt-in.** Previously Venvy
  would run `apt-get install` on boot to provision `python3-venv` and `cargo`.
  It now probes and reports the exact command to run; set
  `VENVY_AUTO_INSTALL_SYSTEM_DEPS=1` for the old behaviour. A server process
  should not silently mutate host packages.
- **Migrations** are documented as append-only, with the reasoning attached.
- **Logging** uses per-module loggers and lazy `%s` formatting, and is
  configured only by the CLI — importing Venvy no longer reconfigures logging
  in the host process.

### Fixed

- **The editor corrupted line endings on non-Unix hosts.** Saving a file went
  through the platform's newline translation, rewriting every LF as CRLF. The
  save path now writes exactly the bytes the editor sent.
- **The CLI crashed on its own banner** when stdout could not encode its
  box-drawing characters (a non-UTF-8 console). It now falls back to ASCII.
- **Blocking filesystem calls on the event loop** during log rotation and
  terminal setup now go through a thread.
- **A stale `.venvy_key` permission** is re-asserted to `0600` on every
  startup, so a restore from backup cannot leave it world-readable.
- **Deleting a user whose session was live** left the session row behind; the
  session is now invalidated when its account is gone.
- Several over-broad `except (SpecificError, Exception)` clauses — which
  silently caught everything — narrowed to what they meant to catch.

### Security

- Internal settings keys are no longer readable or writable through the
  settings API. A pending TOTP secret was previously reachable at
  `GET /api/system/settings/totp_pending_<id>` by an admin mid-enrolment.
- `Retry-After` is now sent alongside the non-standard `X-Retry-After` on
  rate-limit responses.
- Added `base-uri` and `form-action` to the Content-Security-Policy.
- Dropped the deprecated `X-XSS-Protection` header, whose filter is a known
  source of vulnerabilities in the browsers that still honour it.
- Path containment, upload-name validation and entrypoint validation are
  consolidated into `core/paths.py` so there is one place to review, with
  tests including symlink escape.

### Migration from 3.x

The database schema is unchanged and migrates in place; point the new version
at the same directory.

- `python main.py` becomes `venvy` (or `python -m venvy`).
- `main.py`, `index.html` and `emulator.py` are gone from the repository root.
  The emulator is now `tools/dev_emulator.py`.
- The working directory still holds `data.db`, `apps/` and `.venvy_key`;
  `VENVY_DATA_DIR` now overrides it.
- If you relied on Venvy installing `python3-venv` or `cargo` for you, either
  install them yourself or set `VENVY_AUTO_INSTALL_SYSTEM_DEPS=1`.

[4.0.0]: https://github.com/nikolaus018/venvy/releases/tag/v4.0.0
