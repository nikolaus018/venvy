"""Venvy — a self-hosted application manager.

The package is laid out by concern, lowest layer first:

    config      paths, environment-driven settings, logging setup
    crypto      the local encryption key and secret encrypt/decrypt
    models      database tables
    db          engine, sessions, schema migrations
    audit       the audit trail (database + log)
    runtimes    what each runtime runs, its entrypoint rules and package managers
    shared_env  environment variables every app gets
    notify      notification channels (webhook, Slack, Discord, ntfy, Telegram, email)
    monitor     health checks, resource history, disk usage, alerts
    auth        passwords, TOTP, sessions, API tokens, roles, per-app access, rate limiting, client IP
    safefs      path checks and symlink-safe file access inside app directories
    isolation   per-app Unix users
    jobs        background jobs and the task registry
    process     app lifecycle: start/stop, supervisor, logs, ports, metrics
    apps        app naming rules and the API representation of an app
    terminal    the in-browser PTY terminal
    uploads     upload limits and chunk temp files
    backup      full backups and in-place restore
    remote_backup  copying a backup off-site: S3-compatible, WebDAV, scp
    cron        five-field cron expressions
    scheduler   per-app jobs and scheduled backups
    updates     updating Venvy itself from the UI (through the installer's root unit)
    deploy      git deploys, build step, releases and rollback, push webhooks
    proxy       serving an app on its own domain (HTTP and WebSocket)
    cloudflare  Cloudflare API helpers for tunnel routes
    settings    storage rules for system settings (secret / reserved / encrypted)
    startup     host tool checks run at boot
    webui       assembles the HTML document from web/index.html + web/partials/
    api/        HTTP route handlers, one router per resource:
                auth, users, apps, files, system, cloudflare, terminal,
                deploy, cron, ops, tokens, logs, updates
    app         the FastAPI application: lifespan, middleware, static routes

A module only imports from modules above it. `main.py` at the repository
root is the entry point (`python main.py`).
"""

__version__ = "4.5.1"
