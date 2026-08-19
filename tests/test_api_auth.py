"""Authentication, session handling, CSRF and the login rate limiter."""

from __future__ import annotations

import time

from venvy.core import totp


class TestSetup:
    def test_reports_no_users_before_setup(self, client):
        body = client.get("/api/auth/status").json()
        assert body["user_exists"] is False
        assert body["authenticated"] is False

    def test_first_user_becomes_admin(self, client):
        response = client.post(
            "/api/auth/setup", json={"username": "root", "password": "goodpass123"}
        )
        assert response.status_code == 201
        assert response.json()["role"] == "admin"

    def test_setup_signs_the_user_in(self, admin_client, admin_username):
        body = admin_client.get("/api/auth/status").json()
        assert body["authenticated"] is True
        assert body["username"] == admin_username

    def test_setup_can_only_run_once(self, admin_client):
        """The bootstrap endpoint must not become a way to add a second admin."""
        response = admin_client.post(
            "/api/auth/setup", json={"username": "second", "password": "goodpass123"}
        )
        assert response.status_code == 400

    def test_setup_enforces_password_policy(self, client):
        response = client.post("/api/auth/setup", json={"username": "root", "password": "weak"})
        assert response.status_code == 400


class TestLogin:
    def test_valid_credentials(self, admin_client, admin_username, admin_password):
        admin_client.post("/api/auth/logout")
        response = admin_client.post(
            "/api/auth/login", json={"username": admin_username, "password": admin_password}
        )
        assert response.status_code == 200
        assert response.json()["role"] == "admin"

    def test_wrong_password_is_rejected(self, admin_client, admin_username):
        admin_client.post("/api/auth/logout")
        response = admin_client.post(
            "/api/auth/login", json={"username": admin_username, "password": "wrongpass1"}
        )
        assert response.status_code == 401

    def test_unknown_user_is_rejected(self, admin_client):
        admin_client.post("/api/auth/logout")
        response = admin_client.post(
            "/api/auth/login", json={"username": "ghost", "password": "whatever12"}
        )
        assert response.status_code == 401

    def test_unknown_user_and_wrong_password_look_the_same(self, admin_client, admin_username):
        """The response must not reveal whether an account exists."""
        admin_client.post("/api/auth/logout")
        unknown = admin_client.post(
            "/api/auth/login", json={"username": "ghost", "password": "whatever12"}
        )
        wrong = admin_client.post(
            "/api/auth/login", json={"username": admin_username, "password": "whatever12"}
        )
        assert unknown.status_code == wrong.status_code
        assert unknown.json()["detail"] == wrong.json()["detail"]

    def test_logout_invalidates_the_session(self, admin_client):
        assert admin_client.get("/api/apps").status_code == 200
        admin_client.post("/api/auth/logout")
        assert admin_client.get("/api/apps").status_code == 401

    def test_logout_without_a_session_still_succeeds(self, client):
        assert client.post("/api/auth/logout").status_code == 200

    def test_rejects_a_forged_session_cookie(self, client):
        client.cookies.set("session", "not-a-real-token")
        assert client.get("/api/apps").status_code == 401


class TestRateLimiting:
    def test_locks_out_after_repeated_failures(
        self, admin_client, settings, admin_username, admin_password
    ):
        admin_client.post("/api/auth/logout")
        for _ in range(settings.login_max_attempts):
            admin_client.post(
                "/api/auth/login", json={"username": admin_username, "password": "wrongpass1"}
            )

        response = admin_client.post(
            "/api/auth/login", json={"username": admin_username, "password": admin_password}
        )
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    def test_successful_login_clears_the_counter(
        self, admin_client, settings, admin_username, admin_password
    ):
        admin_client.post("/api/auth/logout")
        for _ in range(settings.login_max_attempts - 1):
            admin_client.post(
                "/api/auth/login", json={"username": admin_username, "password": "wrongpass1"}
            )
        assert (
            admin_client.post(
                "/api/auth/login", json={"username": admin_username, "password": admin_password}
            ).status_code
            == 200
        )

        # Having reset the counter, a fresh failure must not trip the lockout.
        admin_client.post("/api/auth/logout")
        assert (
            admin_client.post(
                "/api/auth/login", json={"username": admin_username, "password": "wrongpass1"}
            ).status_code
            == 401
        )


class TestCsrf:
    def test_mutation_without_the_header_is_rejected(self, admin_client):
        """This is the check the browser client satisfies automatically."""
        response = admin_client.request(
            "POST",
            "/api/users",
            json={"username": "bob", "password": "goodpass123"},
            headers={"x-csrf-token": ""},
        )
        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]

    def test_mismatched_token_is_rejected(self, admin_client):
        response = admin_client.request(
            "POST",
            "/api/users",
            json={"username": "bob", "password": "goodpass123"},
            headers={"x-csrf-token": "a-different-token"},
        )
        assert response.status_code == 403

    def test_safe_methods_need_no_token(self, admin_client):
        assert admin_client.get("/api/apps").status_code == 200

    def test_cookie_is_issued_on_first_request(self, client):
        assert client.cookies.get("csrf_token")


class TestPasswordChange:
    def test_changes_the_password(self, admin_client, admin_username, admin_password):
        response = admin_client.put(
            "/api/auth/password",
            json={
                "current_password": admin_password,
                "password": "brandnew456",
                "password_confirm": "brandnew456",
            },
        )
        assert response.status_code == 200

        admin_client.post("/api/auth/logout")
        assert (
            admin_client.post(
                "/api/auth/login", json={"username": admin_username, "password": "brandnew456"}
            ).status_code
            == 200
        )

    def test_requires_the_current_password(self, admin_client):
        response = admin_client.put(
            "/api/auth/password",
            json={
                "current_password": "notitatall",
                "password": "brandnew456",
                "password_confirm": "brandnew456",
            },
        )
        assert response.status_code == 401

    def test_requires_confirmation_to_match(self, admin_client, admin_password):
        response = admin_client.put(
            "/api/auth/password",
            json={
                "current_password": admin_password,
                "password": "brandnew456",
                "password_confirm": "different456",
            },
        )
        assert response.status_code == 400

    def test_enforces_the_password_policy(self, admin_client, admin_password):
        response = admin_client.put(
            "/api/auth/password",
            json={
                "current_password": admin_password,
                "password": "weak",
                "password_confirm": "weak",
            },
        )
        assert response.status_code == 400


class TestTotp:
    def _enrol(self, admin_client) -> str:
        """Complete TOTP enrolment and return the secret."""
        secret = admin_client.get("/api/auth/totp/setup").json()["secret"]
        code = totp._hotp(secret, int(time.time()) // totp.PERIOD_SECONDS)
        assert (
            admin_client.post("/api/auth/totp/setup/confirm", json={"totp_code": code}).status_code
            == 200
        )
        return secret

    def test_setup_returns_a_secret_and_uri(self, admin_client):
        body = admin_client.get("/api/auth/totp/setup").json()
        assert body["secret"]
        assert body["uri"].startswith("otpauth://totp/")

    def test_setup_alone_does_not_enable_totp(self, admin_client):
        admin_client.get("/api/auth/totp/setup")
        assert admin_client.get("/api/auth/status").json()["totp_enabled"] is False

    def test_confirm_with_a_wrong_code_is_rejected(self, admin_client):
        admin_client.get("/api/auth/totp/setup")
        response = admin_client.post("/api/auth/totp/setup/confirm", json={"totp_code": "000000"})
        assert response.status_code == 400

    def test_confirm_without_a_pending_setup_is_rejected(self, admin_client):
        response = admin_client.post("/api/auth/totp/setup/confirm", json={"totp_code": "123456"})
        assert response.status_code == 400

    def test_enrolment_enables_totp(self, admin_client):
        self._enrol(admin_client)
        assert admin_client.get("/api/auth/status").json()["totp_enabled"] is True

    def test_login_then_requires_a_code(self, admin_client, admin_username, admin_password):
        self._enrol(admin_client)
        admin_client.post("/api/auth/logout")

        response = admin_client.post(
            "/api/auth/login", json={"username": admin_username, "password": admin_password}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "totp_required"

    def test_login_succeeds_with_a_valid_code(self, admin_client, admin_username, admin_password):
        secret = self._enrol(admin_client)
        admin_client.post("/api/auth/logout")

        code = totp._hotp(secret, int(time.time()) // totp.PERIOD_SECONDS)
        response = admin_client.post(
            "/api/auth/login",
            json={"username": admin_username, "password": admin_password, "totp_code": code},
        )
        assert response.status_code == 200

    def test_disable_requires_the_password(self, admin_client):
        self._enrol(admin_client)
        assert (
            admin_client.post("/api/auth/totp/disable", json={"password": "wrongpass1"}).status_code
            == 401
        )

    def test_disable_turns_totp_off(self, admin_client, admin_password):
        self._enrol(admin_client)
        assert (
            admin_client.post(
                "/api/auth/totp/disable", json={"password": admin_password}
            ).status_code
            == 200
        )
        assert admin_client.get("/api/auth/status").json()["totp_enabled"] is False

    def test_pending_secret_is_not_readable_via_the_settings_api(self, admin_client):
        """A mid-enrolment secret is credential material, not a setting."""
        admin_client.get("/api/auth/totp/setup")
        response = admin_client.get("/api/system/settings/totp_pending_1")
        assert response.status_code == 403
