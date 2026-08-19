# syntax=docker/dockerfile:1

# ── Build stage ──────────────────────────────────────────────────────────────
# Wheels are built here so the runtime image carries no compiler.
FROM python:3.12-slim AS build

WORKDIR /build
RUN python -m pip install --no-cache-dir build

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m build --wheel --outdir /dist


# ── Runtime stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Venvy manages other applications, so the image needs the toolchains those
# applications are built with:
#   python3-venv  — every Python app gets its own virtualenv
#   cargo/rustc   — Rust apps
#   procps        — psutil and the /proc fallbacks
#   bash          — the jailed terminal
#   git, curl     — routinely needed by the apps being managed
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        bash \
        ca-certificates \
        curl \
        git \
        procps \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Venvy launches the apps it manages as its own user, so it must not be root.
RUN useradd --create-home --shell /bin/bash --uid 10001 venvy

COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl psutil \
    && rm -rf /tmp/*.whl

# All mutable state lives here: the database, the apps tree, backups and the
# encryption key. Mount a volume or the lot is lost when the container goes.
ENV VENVY_DATA_DIR=/data
RUN mkdir -p /data && chown venvy:venvy /data
VOLUME ["/data"]

USER venvy
WORKDIR /data

EXPOSE 54783
ENV VENVY_HOST=0.0.0.0 \
    VENVY_PORT=54783 \
    PYTHONUNBUFFERED=1

# Probes the database, so a wedged instance reports unhealthy rather than a
# false OK.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:54783/api/health || exit 1

ENTRYPOINT ["venvy"]
