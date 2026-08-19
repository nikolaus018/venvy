# Contributing

Thanks for taking a look. Issues and pull requests are both welcome.

## Getting set up

```bash
git clone https://github.com/nikolaus018/venvy.git
cd venvy
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,metrics]"
npm install          # frontend linting only; the app needs no build step
pre-commit install
```

Run the checks:

```bash
pytest                              # tests
ruff check src tests tools          # lint
ruff format src tests tools         # format
mypy                                # types
npm run lint                        # frontend lint
npm run check:modules               # frontend module graph
```

CI runs all of these, on Python 3.11, 3.12 and 3.13, plus one run without
`psutil` to prove the `/proc` fallbacks still work.

### Working on the UI without a Linux host

```bash
python tools/dev_emulator.py
```

This serves the real frontend against fabricated data — no database, no
processes, no PTY. Good enough for anything visual.

## The shape of the codebase

```
src/venvy/
├── core/       no Venvy imports, no framework — pure helpers
├── db/         models, engine, migrations
├── security/   auth, roles, rate limiting, middleware
├── services/   process lifecycle, supervision, terminal, Cloudflare
├── api/        one router per resource; thin, delegating to services
└── web/        frontend: markup, stylesheets, ES modules
```

The dependency direction is one-way: `api → services → db → core`. Nothing in
`core/` imports FastAPI or SQLModel, which is what keeps it importable and
testable on any platform.

Where to put a change:

- **A new endpoint** — a router in `api/`, with the logic in `services/`.
- **New business logic** — `services/`, not the route handler.
- **A pure helper** — `core/`, if it needs nothing from Venvy.
- **A schema change** — `db/models.py`, plus an *append-only* entry in
  `db/migrations.py`. Never edit or reorder an existing migration; a deployed
  database may already have applied it.
- **Configuration** — `config.py`. No other module reads `os.environ`.

## Style

Ruff handles formatting and linting; run it rather than hand-formatting.
Beyond that:

- **Type-annotate new code.** `mypy` runs in strict-ish mode.
- **Docstrings explain *why*.** What the code does is usually visible; the
  reason it does it that way often is not. The comment about `X-Forwarded-For`
  in `security/dependencies.py` is the model.
- **Name things fully.** `app_id`, not `aid`.
- **Catch specific exceptions**, except at supervisor and background-task
  boundaries, where a broad catch is deliberate and should say so.

## Tests

Every behavioural change needs a test. The suite is organised by surface:

| File | Covers |
|---|---|
| `test_core.py` | pure helpers — passwords, TOTP, paths, time |
| `test_api_auth.py` | sign-in, sessions, CSRF, TOTP, rate limiting |
| `test_api_authz.py` | the role matrix and the IP allowlist |
| `test_api_apps.py` | app config, file operations, logs |
| `test_services.py` | crypto, Cloudflare helpers, supervisor policy |

Useful fixtures live in `conftest.py`: `client`, `admin_client` (signed in),
`make_app` (an app row backed by a real directory), and `settings` (an isolated
data directory per test).

Two conventions worth following:

- Test names read as sentences: `test_a_manual_stop_outranks_the_always_policy`.
- Security-relevant behaviour gets an explicit test, and the docstring says
  what the rule protects.

## Frontend

The frontend is native ES modules with **no bundler**. That means:

- Imports must be relative and end in `.js`.
- Shared state lives in `js/core/state.js` as `state.X`; anything used by one
  feature stays in that feature's module.
- If you add an inline `onclick` in `index.html`, add the function to the
  `Object.assign(window, …)` bridge in `js/main.js`. `npm run check:modules`
  fails if you forget, which is the whole reason it exists.

## Pull requests

1. Branch off `main`.
2. Keep the change focused; unrelated cleanups are easier to review separately.
3. Make sure the checks above pass locally.
4. Describe what changed and why. Screenshots help for UI changes.

## Reporting security issues

Not through a public issue — see [SECURITY.md](SECURITY.md).
