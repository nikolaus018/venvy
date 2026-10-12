"""SQLite engine, sessions and schema migrations."""
import logging
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from venvy.config import (
    APP_LOGS_DIR,
    APPS_BASE_DIR,
    BACKUP_DIR,
    DATABASE_URL,
    DB_FILE,
    _UPLOAD_TMP_DIR,
    _VENVY_KEY_FILE,
)
from venvy.crypto import _ENC_PREFIX, _encrypt_secret
from venvy.models import User


engine = create_engine(
    DATABASE_URL, 
    connect_args={"check_same_thread": False, "timeout": 20}, 
    echo=False
)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=-10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_and_tables() -> None:
    """Create missing tables and columns, and bring older data up to date.
    Called once at startup; every step is safe to repeat."""
    SQLModel.metadata.create_all(engine)
    
    # Simple migration logic for existing DBs
    migrations = [
        "ALTER TABLE app ADD COLUMN runtime VARCHAR DEFAULT 'python'",
        "ALTER TABLE app ADD COLUMN url_source_pref VARCHAR DEFAULT 'local'",
        "ALTER TABLE cfroute ADD COLUMN local_port INTEGER",
        "ALTER TABLE app ADD COLUMN manual_ports VARCHAR DEFAULT '[]'",
        "ALTER TABLE app ADD COLUMN memory_limit_mb INTEGER DEFAULT 512",
        "ALTER TABLE app ADD COLUMN cpu_limit_percent INTEGER DEFAULT 50",
        "ALTER TABLE app ADD COLUMN systemd_scope VARCHAR",
        "ALTER TABLE app ADD COLUMN pid_start VARCHAR",
        "ALTER TABLE usersession ADD COLUMN key_hash VARCHAR",
        "ALTER TABLE user ADD COLUMN totp_last_counter INTEGER",
    ]
    
    with engine.begin() as conn:
        for sql in migrations:
            try:
                conn.exec_driver_sql(sql)
            except Exception as e:
                # Silently ignore "duplicate column" errors (expected on subsequent runs).
                # Anything else is a genuine, unexpected migration failure an operator
                # would want to know about, so it's still logged at warning level.
                err_msg = str(e).lower()
                if "duplicate column" not in err_msg and "already exists" not in err_msg:
                    logging.warning(f"Migration failed: {sql} -> {e}")

        # The `role` column is added separately so we can tell whether it was *just*
        # added (i.e. this is an upgrade from a pre-role schema) and, if so, backfill
        # it once from the legacy `is_admin` boolean. On a brand-new DB the column
        # already exists (created by create_all() above), so this ALTER is a no-op
        # duplicate-column error and no backfill runs.
        role_col_is_new = False
        try:
            conn.exec_driver_sql("ALTER TABLE user ADD COLUMN role VARCHAR DEFAULT 'operator'")
            role_col_is_new = True
        except Exception as e:
            err_msg = str(e).lower()
            if "duplicate column" not in err_msg and "already exists" not in err_msg:
                logging.warning(f"Migration failed: add user.role -> {e}")

        if role_col_is_new:
            try:
                cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(user)").fetchall()]
                if "is_admin" in cols:
                    conn.exec_driver_sql(
                        "UPDATE user SET role = CASE WHEN is_admin = 1 THEN 'admin' ELSE 'operator' END"
                    )
                    logging.info("Migrated existing users from is_admin to the new role-based access model.")
            except Exception as e:
                logging.warning(f"Role backfill from legacy is_admin failed (existing users default to 'operator'): {e}")

        # Sessions from before hashed tokens + auth keys can never validate
        # again; drop them rather than let them sit until expiry.
        conn.exec_driver_sql("DELETE FROM usersession WHERE key_hash IS NULL")

        # The GitHub integration is gone; don't leave its stored token behind.
        conn.exec_driver_sql(
            "DELETE FROM systemsetting WHERE key IN ('gh_token', 'gh_client_id', 'gh_username', 'gh_avatar')"
        )

    # TOTP secrets used to be stored as-is; anyone who could read data.db (or a
    # backup of it) could generate every user's codes. Encrypt what's there.
    with Session(engine) as session:
        plain = [u for u in session.exec(select(User)).all()
                 if u.totp_secret and not u.totp_secret.startswith(_ENC_PREFIX)]
        for u in plain:
            u.totp_secret = _encrypt_secret(u.totp_secret)
            session.add(u)
        if plain:
            session.commit()
            logging.info(f"Encrypted {len(plain)} stored TOTP secret(s).")

    APPS_BASE_DIR.mkdir(exist_ok=True)
    _lock_down_data_files()


def _lock_down_data_files() -> None:
    """Make Venvy's own state readable by the Venvy user only."""
    for path, mode in ((DB_FILE, 0o600), (_VENVY_KEY_FILE, 0o600), (BACKUP_DIR, 0o700),
                       (APP_LOGS_DIR, 0o700), (_UPLOAD_TMP_DIR, 0o700)):
        try:
            if path.exists():
                path.chmod(mode)
        except OSError:
            pass
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(DB_FILE) + suffix).chmod(0o600)
        except OSError:
            pass


def get_session():
    with Session(engine) as session:
        yield session
