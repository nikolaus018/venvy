"""Venvy — a self-hosted application manager.

The package is laid out by concern:

    config      paths, environment-driven settings, logging setup
    crypto      the local encryption key and secret encrypt/decrypt
    models      database tables
    db          engine, sessions, schema migrations
    auth        passwords, TOTP, sessions, roles, rate limiting, client IP
    safefs      symlink-safe file access inside app directories
    isolation   per-app Unix users
    process     app lifecycle: start/stop, supervisor, logs, ports, metrics
    terminal    the in-browser PTY terminal
    backup      full backups and in-place restore
    cloudflare  Cloudflare API helpers for tunnel routes
    github      GitHub sign-in state and git helpers
    api/        HTTP route handlers, one router per resource
    app         the FastAPI application: lifespan, middleware, static routes

`main.py` at the repository root is the entry point (`python main.py`).
"""
