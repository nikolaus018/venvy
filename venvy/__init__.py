"""Venvy — a self-hosted application manager.

The package is laid out by concern, lowest layer first:

    config      paths, environment-driven settings, logging setup
    crypto      the local encryption key and secret encrypt/decrypt
    audit       audit-trail logging
    models      database tables
    db          engine, sessions, schema migrations
    auth        passwords, TOTP, sessions, roles, rate limiting, client IP
    safefs      path checks and symlink-safe file access inside app directories
    isolation   per-app Unix users
    jobs        background jobs and the task registry
    process     app lifecycle: start/stop, supervisor, logs, ports, metrics
    apps        app naming rules and the API representation of an app
    terminal    the in-browser PTY terminal
    uploads     upload limits and chunk temp files
    backup      full backups and in-place restore
    cloudflare  Cloudflare API helpers for tunnel routes
    settings    storage rules for system settings (secret / reserved / encrypted)
    startup     host tool checks run at boot
    webui       assembles the HTML document from web/index.html + web/partials/
    api/        HTTP route handlers, one router per resource:
                auth, users, apps, files, system, cloudflare, terminal
    app         the FastAPI application: lifespan, middleware, static routes

A module only imports from modules above it. `main.py` at the repository
root is the entry point (`python main.py`).
"""
