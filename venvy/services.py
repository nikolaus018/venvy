"""Managed services: a database or cache made in one click and handed to apps.

A service is a docker app made from a template (service_template), with
generated credentials in its own (encrypted) environment, its data in its
$APP_DATA_DIR (mounted at /data), published on 127.0.0.1 only. An app that
uses a service (a ServiceBinding) gets its connection variables at start —
DATABASE_URL, REDIS_URL, … (or PREFIX_URL with a prefix).

Before every backup, running services dump themselves into their data folder
(pg_dumpall, mariadb-dump, mongodump, redis SAVE), so the backup holds a
consistent copy and not only the live files.
"""
import asyncio
import contextlib
import json
import logging
import re
import secrets
from typing import Dict, List
from urllib.parse import quote

from sqlmodel import Session, select

from venvy.crypto import _try_decrypt_secret
from venvy.db import engine
from venvy.models import App, ServiceBinding

PREFIX_RE = re.compile(r"[A-Z][A-Z0-9_]{0,30}")


def _pw() -> str:
    return secrets.token_urlsafe(24).replace("-", "a").replace("_", "b")


TEMPLATES: Dict[str, dict] = {
    "postgres": {
        "label": "PostgreSQL", "image": "postgres:17-alpine", "port": 5432,
        "env": lambda: {"POSTGRES_USER": "app", "POSTGRES_PASSWORD": _pw(), "POSTGRES_DB": "app",
                        "PGDATA": "/data/pgdata"},
        "health": "tcp",
        "dump": ["sh", "-c", 'pg_dumpall -U "$POSTGRES_USER" > /data/venvy-dump.sql'],
    },
    "mariadb": {
        "label": "MariaDB / MySQL", "image": "mariadb:11", "port": 3306,
        "env": lambda: {"MARIADB_USER": "app", "MARIADB_PASSWORD": _pw(), "MARIADB_DATABASE": "app",
                        "MARIADB_ROOT_PASSWORD": _pw()},
        "args": "--datadir=/data/mysql",
        "health": "tcp",
        "dump": ["sh", "-c", 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --all-databases --single-transaction > /data/venvy-dump.sql'],
    },
    "redis": {
        "label": "Redis", "image": "redis:7-alpine", "port": 6379,
        "env": lambda: {"REDIS_PASSWORD": _pw()},
        "args": "sh -c 'exec redis-server --dir /data --appendonly yes --requirepass \"$$REDIS_PASSWORD\"'",
        "health": "tcp",
        "dump": ["sh", "-c", 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning SAVE'],
    },
    "valkey": {
        "label": "Valkey", "image": "valkey/valkey:8-alpine", "port": 6379,
        "env": lambda: {"REDIS_PASSWORD": _pw()},
        "args": "sh -c 'exec valkey-server --dir /data --appendonly yes --requirepass \"$$REDIS_PASSWORD\"'",
        "health": "tcp",
        "dump": ["sh", "-c", 'valkey-cli -a "$REDIS_PASSWORD" --no-auth-warning SAVE'],
    },
    "mongodb": {
        "label": "MongoDB", "image": "mongo:8", "port": 27017,
        "env": lambda: {"MONGO_INITDB_ROOT_USERNAME": "app", "MONGO_INITDB_ROOT_PASSWORD": _pw()},
        "args": "mongod --dbpath /data/db --bind_ip_all",
        "health": "tcp",
        "dump": ["sh", "-c", 'mongodump -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --archive=/data/venvy-dump.archive'],
    },
    "meilisearch": {
        "label": "Meilisearch", "image": "getmeili/meilisearch:v1.12", "port": 7700,
        "env": lambda: {"MEILI_MASTER_KEY": _pw(), "MEILI_DB_PATH": "/data/data.ms", "MEILI_ENV": "production"},
        "health": "http", "health_path": "/health",
    },
    "minio": {
        "label": "MinIO (S3 object storage)", "image": "minio/minio:latest", "port": 9000,
        "env": lambda: {"MINIO_ROOT_USER": "app" + secrets.token_hex(4), "MINIO_ROOT_PASSWORD": _pw()},
        "args": "server /data",
        "health": "http", "health_path": "/minio/health/live",
    },
    "rabbitmq": {
        "label": "RabbitMQ", "image": "rabbitmq:4-alpine", "port": 5672,
        "env": lambda: {"RABBITMQ_DEFAULT_USER": "app", "RABBITMQ_DEFAULT_PASS": _pw(),
                        "RABBITMQ_MNESIA_BASE": "/data/mnesia"},
        "health": "tcp",
    },
}


def describe() -> List[dict]:
    return [{"key": k, "label": t["label"], "image": t["image"], "port": t["port"]} for k, t in TEMPLATES.items()]


def new_service_fields(template: str) -> dict:
    """App fields for a new service of *template* (raises KeyError if unknown)."""
    t = TEMPLATES[template]
    return {
        "runtime": "docker", "entrypoint": t["image"], "container_port": t["port"],
        "start_command": t.get("args"), "service_template": template, "bind_public": False,
        "restart_policy": "always", "start_on_boot": True, "health_type": t.get("health", "tcp"),
        "healthcheck_path": t.get("health_path"), "env": t["env"](), "sandbox": False,
    }


def _env_of(app: App) -> Dict[str, str]:
    ok, plain = _try_decrypt_secret(app.env_json or "")
    if not ok:
        return {}
    with contextlib.suppress(ValueError):
        d = json.loads(plain or "{}")
        if isinstance(d, dict):
            return {str(k): str(v) for k, v in d.items()}
    return {}


def connection_env(svc: App, prefix: str = "") -> Dict[str, str]:
    """The variables an app gets for one service."""
    e = _env_of(svc)
    port = svc.port
    host = "127.0.0.1"
    if not port:
        return {}
    t = svc.service_template
    p = (prefix + "_") if prefix else ""
    out: Dict[str, str] = {}
    if t == "postgres":
        u, pw, db = e.get("POSTGRES_USER", "app"), e.get("POSTGRES_PASSWORD", ""), e.get("POSTGRES_DB", "app")
        out[f"{p}DATABASE_URL" if p else "DATABASE_URL"] = f"postgresql://{quote(u)}:{quote(pw)}@{host}:{port}/{quote(db)}"
        if not p:
            out.update(PGHOST=host, PGPORT=str(port), PGUSER=u, PGPASSWORD=pw, PGDATABASE=db)
    elif t == "mariadb":
        u, pw, db = e.get("MARIADB_USER", "app"), e.get("MARIADB_PASSWORD", ""), e.get("MARIADB_DATABASE", "app")
        out[f"{p}DATABASE_URL" if p else "DATABASE_URL"] = f"mysql://{quote(u)}:{quote(pw)}@{host}:{port}/{quote(db)}"
        if not p:
            out.update(MYSQL_HOST=host, MYSQL_PORT=str(port), MYSQL_USER=u, MYSQL_PASSWORD=pw, MYSQL_DATABASE=db)
    elif t in ("redis", "valkey"):
        out[f"{p}REDIS_URL" if p else "REDIS_URL"] = f"redis://:{quote(e.get('REDIS_PASSWORD', ''))}@{host}:{port}/0"
    elif t == "mongodb":
        u, pw = e.get("MONGO_INITDB_ROOT_USERNAME", "app"), e.get("MONGO_INITDB_ROOT_PASSWORD", "")
        out[f"{p}MONGODB_URL" if p else "MONGODB_URL"] = f"mongodb://{quote(u)}:{quote(pw)}@{host}:{port}/?authSource=admin"
    elif t == "meilisearch":
        out[f"{p}MEILI_URL"] = f"http://{host}:{port}"
        out[f"{p}MEILI_MASTER_KEY"] = e.get("MEILI_MASTER_KEY", "")
    elif t == "minio":
        out[f"{p}S3_ENDPOINT"] = f"http://{host}:{port}"
        out[f"{p}S3_ACCESS_KEY"] = out[f"{p}AWS_ACCESS_KEY_ID"] = e.get("MINIO_ROOT_USER", "")
        out[f"{p}S3_SECRET_KEY"] = out[f"{p}AWS_SECRET_ACCESS_KEY"] = e.get("MINIO_ROOT_PASSWORD", "")
    elif t == "rabbitmq":
        out[f"{p}AMQP_URL" if p else "AMQP_URL"] = (f"amqp://{quote(e.get('RABBITMQ_DEFAULT_USER', 'app'))}:"
                                                    f"{quote(e.get('RABBITMQ_DEFAULT_PASS', ''))}@{host}:{port}/")
    return out


def binding_env(app_id: int) -> Dict[str, str]:
    """Connection variables for every service app *app_id* uses."""
    with Session(engine) as s:
        rows = s.exec(select(ServiceBinding).where(ServiceBinding.app_id == app_id)).all()
        out: Dict[str, str] = {}
        for b in rows:
            svc = s.get(App, b.service_app_id)
            if svc is not None and svc.service_template:
                out.update(connection_env(svc, b.prefix))
    return out


async def dump_all(log=lambda m: logging.info(m)) -> None:
    """Have every running service write a dump into its data folder (before a backup)."""
    from venvy import runtimes
    with Session(engine) as s:
        svcs = [a for a in s.exec(select(App).where(App.service_template.is_not(None))).all()
                if a.status == "running"]
    for svc in svcs:
        t = TEMPLATES.get(svc.service_template or "")
        if not t or not t.get("dump"):
            continue
        argv = ["docker", "exec", runtimes.docker_name(svc.id)] + t["dump"]
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                                                        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
            _, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
            if proc.returncode != 0:
                log(f"Dump of service '{svc.name}' failed: {err.decode(errors='replace').strip()[-300:]}")
            else:
                log(f"Service '{svc.name}' dumped for the backup.")
        except Exception as exc:
            log(f"Dump of service '{svc.name}' failed: {exc}")
