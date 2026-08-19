"""Shared fixtures.

Every test gets its own data directory, so the database, the encryption key and
the apps tree are all disposable and no test can observe another's state.
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from venvy.config import Settings, configure, get_settings
from venvy.core import crypto
from venvy.db import engine as db_engine
from venvy.security import rate_limit
from venvy.services import registry

#: Skips for behaviour that only exists on POSIX hosts.
requires_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="requires POSIX process semantics"
)

#: Credentials for the admin account created by the ``admin_client`` fixture.
#: Exposed as fixtures rather than imported, so test modules never have to
#: import ``tests`` as a package.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "testpass123"


@pytest.fixture
def admin_username() -> str:
    """Username of the account ``admin_client`` is signed in as."""
    return ADMIN_USERNAME


@pytest.fixture
def admin_password() -> str:
    """Password of the account ``admin_client`` is signed in as."""
    return ADMIN_PASSWORD


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """An isolated data directory for one test."""
    directory = tmp_path / "venvy-data"
    directory.mkdir()
    return directory


@pytest.fixture
def settings(data_dir: Path) -> Iterator[Settings]:
    """Install test settings and reset every module-level cache afterwards."""
    original = get_settings()
    active = configure(
        dataclasses.replace(
            original,
            data_dir=data_dir,
            ip_allowlist=frozenset(),
            allowed_origins=(),
            # A server process must never touch host packages during a test.
            auto_install_system_deps=False,
            use_systemd_scopes=False,
        )
    )
    # Caches keyed to the old data directory would otherwise leak across tests.
    db_engine.dispose_engine()
    crypto.reset_cache()
    rate_limit.reset_all()
    registry.clear_all()

    yield active

    db_engine.dispose_engine()
    crypto.reset_cache()
    rate_limit.reset_all()
    registry.clear_all()
    configure(original)


class VenvyClient(TestClient):
    """A TestClient that echoes the CSRF cookie the way the frontend does.

    The API requires every state-changing request to send the ``csrf_token``
    cookie back in the ``x-csrf-token`` header. The browser client does this in
    its fetch wrapper; without it here, every POST/PUT/DELETE in the suite
    would just be testing that CSRF works.
    """

    def request(self, method: str, url, **kwargs):  # type: ignore[override]
        if method.upper() not in ("GET", "HEAD", "OPTIONS", "TRACE"):
            token = self.cookies.get("csrf_token")
            if token:
                headers = dict(kwargs.pop("headers", None) or {})
                headers.setdefault("x-csrf-token", token)
                kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def client(settings: Settings) -> Iterator[VenvyClient]:
    """A test client with the lifespan running, so migrations have been applied."""
    from venvy.app import create_app

    with VenvyClient(create_app()) as test_client:
        # One safe request so the CSRF cookie is issued before anything mutates.
        test_client.get("/api/health")
        yield test_client


@pytest.fixture
def admin_client(client: VenvyClient) -> VenvyClient:
    """A client signed in as the instance's first admin."""
    response = client.post(
        "/api/auth/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
def make_app(admin_client: VenvyClient, settings: Settings):
    """Return a factory that inserts an App row backed by a real directory.

    Creating apps through the HTTP endpoint would build a real virtualenv,
    which is far too slow for tests that only care about routing and
    permissions.
    """
    from venvy.db.engine import session_scope
    from venvy.db.models import App

    def factory(name: str = "demo", **overrides) -> int:
        app_dir = settings.apps_dir / "1" / name
        (app_dir / "logs").mkdir(parents=True, exist_ok=True)
        fields = {
            "user_id": 1,
            "name": name,
            "slug": name,
            "runtime": "python",
            "app_dir": str(app_dir),
            "venv_path": str(app_dir / "venv"),
            "entrypoint": "main.py",
        }
        fields.update(overrides)
        with session_scope() as session:
            row = App(**fields)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    return factory
