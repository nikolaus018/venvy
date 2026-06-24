import datetime
import logging
import os
from pathlib import Path
from typing import List, Optional, Set

from sqlmodel import Field, Session, SQLModel, create_engine

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None


from app.config import DATABASE_URL, APPS_BASE_DIR

# ---------------------------------------------------------------------------
# Database models
# ---------------------------------------------------------------------------


def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# Tracks exactly when this Venvy instance started up.
_VENVY_BOOT_TIME = utc_now()


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    is_admin: bool = Field(default=False)
    created_at: datetime.datetime = Field(default_factory=utc_now)
    totp_secret: Optional[str] = Field(default=None)  # None = TOTP disabled
    totp_enabled: bool = Field(default=False)


class UserSession(SQLModel, table=True):
    token: str = Field(primary_key=True)
    user_id: int = Field(index=True)
    created_at: datetime.datetime = Field(default_factory=utc_now)
    expires_at: datetime.datetime


class App(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    name: str = Field(index=True)
    slug: str
    runtime: str = Field(default="python")
    created_at: datetime.datetime = Field(default_factory=utc_now)
    updated_at: datetime.datetime = Field(default_factory=utc_now)

    # Runtime state
    status: str = Field(default="stopped")
    pid: Optional[int] = Field(default=None)

    # Config
    venv_path: str
    app_dir: str
    entrypoint: str
    start_on_boot: bool = Field(default=False)
    restart_policy: str = Field(default="never")  # never | on-failure | always

    # History
    last_exit_code: Optional[int] = Field(default=None)
    last_start_time: Optional[datetime.datetime] = Field(default=None)
    last_stop_time: Optional[datetime.datetime] = Field(default=None)
    # Persisted across Venvy restarts so the "always" restart policy does not
    # immediately re-start apps that were manually stopped before Venvy restarted.
    last_manually_stopped_at: Optional[datetime.datetime] = Field(default=None)

    # Network / environment
    port: Optional[int] = Field(default=None)
    manual_ports: str = Field(default="[]")  # JSON list of integers
    detected_ports: str = Field(default="[]")
    external_url: Optional[str] = Field(default=None)
    env_json: str = Field(default="{}")
    healthcheck_path: Optional[str] = Field(default=None)
    url_source_pref: str = Field(default="local")  # "local" or "public"


class SystemSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = Field(default="")


class CfRoute(SQLModel, table=True):
    """Tracks the Cloudflare public hostname route created for each app."""

    id: Optional[int] = Field(default=None, primary_key=True)
    app_id: int = Field(index=True)
    local_port: Optional[int] = Field(default=None)  # The specific app port mapped
    hostname: str = Field(default="")  # e.g. myapp.example.com
    cf_route_id: Optional[str] = Field(
        default=None
    )  # CF ingress rule id (for deletion)


engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 20}, echo=False
)

from sqlalchemy import event


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_and_tables() -> None:
    """Create all tables. Called once at startup. Schema changes require a fresh DB."""
    SQLModel.metadata.create_all(engine)

    # Simple migration logic for existing DBs
    migrations = [
        "ALTER TABLE app ADD COLUMN runtime VARCHAR DEFAULT 'python'",
        "ALTER TABLE app ADD COLUMN url_source_pref VARCHAR DEFAULT 'local'",
        "ALTER TABLE cfroute ADD COLUMN local_port INTEGER",
        "ALTER TABLE app ADD COLUMN manual_ports VARCHAR DEFAULT '[]'",
    ]

    with engine.begin() as conn:
        for sql in migrations:
            try:
                conn.exec_driver_sql(sql)
            except Exception as e:
                # Silently ignore "duplicate column" errors (expected on subsequent runs)
                err_msg = str(e).lower()
                if (
                    "duplicate column" not in err_msg
                    and "already exists" not in err_msg
                ):
                    logging.warning(f"Migration failed: {sql} -> {e}")

    APPS_BASE_DIR.mkdir(exist_ok=True)


def _psutil_net_connections(kind: str = "tcp"):
    """Wrapper for psutil.net_connections that handles the API change in psutil ≥ 6.0."""
    if psutil is None:
        return []
    try:
        return psutil.net_connections(kind=kind)
    except (AttributeError, Exception):
        return []


def _detect_ports_sync(app_pid: int) -> List[int]:
    """Synchronous function to detect all ports a given PID tree is bound to."""
    pids: List[int] = [app_pid]
    if psutil:
        try:
            pids += [c.pid for c in psutil.Process(app_pid).children(recursive=True)]
        except Exception:
            pass
    else:
        try:
            parent_to_children = {}
            for p in Path("/proc").iterdir():
                if not p.name.isdigit():
                    continue
                try:
                    stat = (p / "stat").read_text().split()
                    ppid = int(stat[3])
                    pid = int(p.name)
                    parent_to_children.setdefault(ppid, []).append(pid)
                except Exception:
                    pass
            queue = [app_pid]
            visited = {app_pid}
            while queue:
                current = queue.pop(0)
                for child in parent_to_children.get(current, []):
                    if child not in visited:
                        visited.add(child)
                        pids.append(child)
                        queue.append(child)
        except Exception:
            pass

    detected_ports: Set[int] = set()

    # ── Primary: /proc fd inode matching ─────────────
    for check_pid in pids:
        try:
            fd_dir = Path(f"/proc/{check_pid}/fd")
            socket_inodes: Set[str] = set()
            for fd_path in fd_dir.iterdir():
                try:
                    link = os.readlink(str(fd_path))
                    if link.startswith("socket:["):
                        socket_inodes.add(link[8:-1])
                except OSError:
                    pass
            if not socket_inodes:
                continue
            for tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
                try:
                    for line in Path(tcp_file).read_text().splitlines()[1:]:
                        parts = line.split()
                        if len(parts) < 10:
                            continue
                        if parts[3] == "0A" and parts[9] in socket_inodes:
                            port_num = int(parts[1].split(":")[1], 16)
                            if port_num > 0:
                                detected_ports.add(port_num)
                except Exception:
                    pass
        except (OSError, PermissionError):
            pass

    # ── Secondary: psutil net_connections ─────────────
    if not detected_ports and psutil:
        pid_set = set(pids)
        for conn in _psutil_net_connections(kind="tcp"):
            if (
                conn.status == "LISTEN"
                and conn.laddr
                and conn.pid is not None
                and conn.pid in pid_set
                and conn.laddr.port > 0
            ):
                detected_ports.add(conn.laddr.port)

    return sorted(list(detected_ports))


def get_session():
    with Session(engine) as session:
        yield session
