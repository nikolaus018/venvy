"""Role-based access control and the IP allowlist.

The role matrix is the security boundary between someone who can read the
dashboard and someone who can execute code on the host, so it is checked
endpoint by endpoint rather than by sampling.
"""

from __future__ import annotations

import dataclasses

import pytest

from venvy.config import configure


@pytest.fixture
def as_role(admin_client, client):
    """Return a helper that creates an account with *role* and signs in as it."""

    def switch(role: str, username: str | None = None):
        name = username or f"{role}-user"
        response = admin_client.post(
            "/api/users", json={"username": name, "password": "rolepass123", "role": role}
        )
        assert response.status_code == 201, response.text
        admin_client.post("/api/auth/logout")
        assert (
            admin_client.post(
                "/api/auth/login", json={"username": name, "password": "rolepass123"}
            ).status_code
            == 200
        )
        return admin_client

    return switch


class TestViewerIsReadOnly:
    def test_viewer_can_list_apps(self, as_role, make_app):
        make_app("readable")
        assert as_role("viewer").get("/api/apps").status_code == 200

    def test_viewer_can_read_logs(self, as_role, make_app):
        app_id = make_app("readable")
        assert as_role("viewer").get(f"/api/apps/{app_id}/logs").status_code == 200

    def test_viewer_cannot_control_an_app(self, as_role, make_app):
        app_id = make_app("guarded")
        response = as_role("viewer").post(f"/api/apps/{app_id}/control?action=start")
        assert response.status_code == 403

    def test_viewer_cannot_edit_files(self, as_role, make_app):
        app_id = make_app("guarded")
        response = as_role("viewer").post(
            f"/api/apps/{app_id}/files/op",
            json={"operation": "create_file", "path": "evil.py"},
        )
        assert response.status_code == 403

    def test_viewer_cannot_install_packages(self, as_role, make_app):
        app_id = make_app("guarded")
        response = as_role("viewer").post(f"/api/apps/{app_id}/packages", json={"name": "requests"})
        assert response.status_code == 403

    def test_viewer_cannot_delete_an_app(self, as_role, make_app):
        app_id = make_app("guarded")
        assert as_role("viewer").delete(f"/api/apps/{app_id}").status_code == 403

    def test_viewer_cannot_clear_logs(self, as_role, make_app):
        app_id = make_app("guarded")
        assert as_role("viewer").delete(f"/api/apps/{app_id}/logs").status_code == 403

    def test_viewer_cannot_see_app_secrets(self, as_role, admin_client, make_app):
        """Environment variables hold real credentials for the hosted app."""
        app_id = make_app("secretive")
        admin_client.put(f"/api/apps/{app_id}", json={"env_vars": {"DB_PASSWORD": "s3cr3t"}})
        apps = as_role("viewer").get("/api/apps").json()
        entry = next(a for a in apps if a["id"] == app_id)
        assert "s3cr3t" not in entry["env_json"]
        assert entry["env_redacted"] is True


class TestOperatorPermissions:
    def test_operator_can_control_apps(self, as_role, make_app):
        app_id = make_app("controllable")
        response = as_role("operator").post(f"/api/apps/{app_id}/control?action=stop")
        assert response.status_code == 200

    def test_operator_can_see_app_secrets(self, as_role, admin_client, make_app):
        app_id = make_app("secretive")
        admin_client.put(f"/api/apps/{app_id}", json={"env_vars": {"TOKEN": "visible"}})
        apps = as_role("operator").get("/api/apps").json()
        entry = next(a for a in apps if a["id"] == app_id)
        assert "visible" in entry["env_json"]

    def test_operator_cannot_manage_users(self, as_role):
        assert as_role("operator").get("/api/users").status_code == 403

    def test_operator_cannot_create_users(self, as_role):
        response = as_role("operator").post(
            "/api/users", json={"username": "sneaky", "password": "goodpass123"}
        )
        assert response.status_code == 403

    def test_operator_cannot_read_system_settings(self, as_role):
        assert as_role("operator").get("/api/system/settings/cf_api_token").status_code == 403

    def test_operator_cannot_take_backups(self, as_role):
        assert as_role("operator").post("/api/system/backup").status_code == 403


class TestAdminPermissions:
    def test_admin_can_manage_users(self, admin_client):
        assert admin_client.get("/api/users").status_code == 200

    def test_admin_can_read_settings(self, admin_client):
        assert admin_client.get("/api/system/settings/cf_api_token").status_code == 200

    def test_admin_can_take_a_backup(self, admin_client, settings):
        response = admin_client.post("/api/system/backup")
        assert response.status_code == 200
        assert response.json()["size_bytes"] > 0

    def test_backup_is_listed_afterwards(self, admin_client):
        admin_client.post("/api/system/backup")
        backups = admin_client.get("/api/system/backups").json()
        assert len(backups) == 1
        assert backups[0]["filename"].startswith("data-")

    def test_admin_cannot_delete_themselves(self, admin_client):
        """Removing the last admin would lock everyone out of user management."""
        me = admin_client.get("/api/users").json()[0]
        assert admin_client.delete(f"/api/users/{me['id']}").status_code == 400

    def test_deleting_a_user_does_not_delete_their_apps(self, admin_client, make_app):
        """Apps belong to the instance, not to the account that uploaded them."""
        app_id = make_app("survivor")
        admin_client.post("/api/users", json={"username": "temp", "password": "goodpass123"})
        temp = next(u for u in admin_client.get("/api/users").json() if u["username"] == "temp")
        assert admin_client.delete(f"/api/users/{temp['id']}").status_code == 200

        app_ids = [a["id"] for a in admin_client.get("/api/apps").json()]
        assert app_id in app_ids


class TestAnonymousAccess:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/apps",
            "/api/users",
            "/api/system/info",
            "/api/system/metrics",
            "/api/system/settings/cf_api_token",
        ],
    )
    def test_protected_endpoints_require_a_session(self, client, path):
        assert client.get(path).status_code == 401

    def test_health_is_public(self, client):
        """An uptime monitor must be able to reach this without signing in."""
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_auth_status_is_public(self, client):
        assert client.get("/api/auth/status").status_code == 200


class TestIpAllowlist:
    def test_blocks_addresses_not_on_the_list(self, settings, data_dir):
        from fastapi.testclient import TestClient

        from venvy.app import create_app

        configure(dataclasses.replace(settings, ip_allowlist=frozenset({"203.0.113.7"})))
        with TestClient(create_app()) as client:
            blocked = client.get("/api/auth/status", headers={"CF-Connecting-IP": "198.51.100.1"})
            assert blocked.status_code == 403

    def test_allows_addresses_on_the_list(self, settings, data_dir):
        from fastapi.testclient import TestClient

        from venvy.app import create_app

        configure(dataclasses.replace(settings, ip_allowlist=frozenset({"203.0.113.7"})))
        with TestClient(create_app()) as client:
            allowed = client.get("/api/auth/status", headers={"CF-Connecting-IP": "203.0.113.7"})
            assert allowed.status_code == 200

    def test_x_forwarded_for_cannot_bypass_the_allowlist(self, settings, data_dir):
        """Cloudflare only appends to XFF, so its first entry is attacker-controlled."""
        from fastapi.testclient import TestClient

        from venvy.app import create_app

        configure(dataclasses.replace(settings, ip_allowlist=frozenset({"203.0.113.7"})))
        with TestClient(create_app()) as client:
            spoofed = client.get(
                "/api/auth/status",
                headers={
                    "X-Forwarded-For": "203.0.113.7",
                    "CF-Connecting-IP": "198.51.100.1",
                },
            )
            assert spoofed.status_code == 403

    def test_health_is_still_reachable_behind_an_allowlist(self, settings, data_dir):
        """Health sits outside the allowlist on purpose, for external monitors."""
        from fastapi.testclient import TestClient

        from venvy.app import create_app

        configure(dataclasses.replace(settings, ip_allowlist=frozenset({"203.0.113.7"})))
        with TestClient(create_app()) as client:
            response = client.get("/api/health", headers={"CF-Connecting-IP": "198.51.100.1"})
            assert response.status_code == 200
