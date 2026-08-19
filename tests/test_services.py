"""Unit tests for the service layer: crypto, rate limiting, Cloudflare, supervision."""

from __future__ import annotations

import datetime
import json

import pytest

from venvy.core import crypto
from venvy.db.models import App
from venvy.services import cloudflare as cf
from venvy.services import registry


class TestCrypto:
    def test_roundtrip(self, settings):
        assert crypto.decrypt_secret(crypto.encrypt_secret("hunter2")) == "hunter2"

    def test_ciphertext_does_not_contain_the_plaintext(self, settings):
        assert "hunter2" not in crypto.encrypt_secret("hunter2")

    def test_ciphertext_is_marked_with_a_version_prefix(self, settings):
        assert crypto.encrypt_secret("x").startswith(crypto.ENC_PREFIX)

    def test_empty_stays_empty(self, settings):
        """Callers use falsiness to mean 'unset'; encryption must preserve that."""
        assert crypto.encrypt_secret("") == ""
        assert crypto.decrypt_secret("") == ""

    def test_encryption_is_non_deterministic(self, settings):
        """Fernet includes a random IV, so equal inputs must differ as ciphertext."""
        assert crypto.encrypt_secret("same") != crypto.encrypt_secret("same")

    def test_legacy_plaintext_passes_through(self, settings):
        """Rows written before encryption existed must keep working."""
        assert crypto.decrypt_secret("plain-old-value") == "plain-old-value"

    def test_undecryptable_value_returns_empty(self, settings):
        """A lost key must fail closed, not raise into a request handler."""
        assert crypto.decrypt_secret(crypto.ENC_PREFIX + "garbage") == ""

    def test_key_file_is_created_on_first_use(self, settings):
        crypto.encrypt_secret("anything")
        assert settings.key_file.exists()

    def test_value_is_unreadable_with_a_different_key(self, settings):
        """Restoring a database without its key must not leak the secrets."""
        ciphertext = crypto.encrypt_secret("topsecret")
        settings.key_file.unlink()
        crypto.reset_cache()
        assert crypto.decrypt_secret(ciphertext) == ""


class TestLoginRateLimiter:
    def test_allows_traffic_below_the_threshold(self, settings):
        from venvy.security.rate_limit import login_limiter

        for _ in range(settings.login_max_attempts - 1):
            login_limiter.record_failure("1.2.3.4")
        login_limiter.check("1.2.3.4")  # must not raise

    def test_locks_out_at_the_threshold(self, settings):
        from fastapi import HTTPException

        from venvy.security.rate_limit import login_limiter

        for _ in range(settings.login_max_attempts):
            login_limiter.record_failure("1.2.3.4")
        with pytest.raises(HTTPException) as exc_info:
            login_limiter.check("1.2.3.4")
        assert exc_info.value.status_code == 429

    def test_lockouts_are_per_client(self, settings):
        from venvy.security.rate_limit import login_limiter

        for _ in range(settings.login_max_attempts):
            login_limiter.record_failure("1.2.3.4")
        login_limiter.check("5.6.7.8")  # a different IP is unaffected

    def test_clear_releases_the_lockout(self, settings):
        from venvy.security.rate_limit import login_limiter

        for _ in range(settings.login_max_attempts):
            login_limiter.record_failure("1.2.3.4")
        login_limiter.clear("1.2.3.4")
        login_limiter.check("1.2.3.4")


class TestCloudflareHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://app.example.com", "app.example.com"),
            ("http://app.example.com/", "app.example.com"),
            ("  APP.Example.COM  ", "app.example.com"),
            ("app.example.com", "app.example.com"),
        ],
    )
    def test_normalize_hostname(self, raw, expected):
        assert cf.normalize_hostname(raw) == expected

    def test_sanitize_strips_whitespace_and_newlines(self):
        assert cf.sanitize_credential("  token\n  ") == "token"

    def test_sanitize_strips_zero_width_characters(self):
        """Pasting from the Cloudflare dashboard routinely drags these along."""
        assert cf.sanitize_credential("to​ken﻿") == "token"

    def test_build_ingress_appends_the_new_rule_before_the_catch_all(self):
        existing = [
            {"hostname": "keep.example.com", "service": "http://127.0.0.1:1"},
            {"service": "http_status:404"},
        ]
        result = cf.build_ingress(
            existing, set(), {"hostname": "new.example.com", "service": "http://127.0.0.1:2"}
        )
        assert [rule.get("hostname") for rule in result] == [
            "keep.example.com",
            "new.example.com",
            None,
        ]

    def test_build_ingress_drops_named_hostnames(self):
        existing = [
            {"hostname": "old.example.com", "service": "http://127.0.0.1:1"},
            {"service": "http_status:404"},
        ]
        result = cf.build_ingress(existing, {"old.example.com"})
        assert result == [{"service": "http_status:404"}]

    def test_build_ingress_supplies_a_catch_all_when_missing(self):
        """Cloudflare rejects an ingress list that does not end in a catch-all."""
        result = cf.build_ingress(
            [], set(), {"hostname": "a.example.com", "service": "http://127.0.0.1:1"}
        )
        assert result[-1] == {"service": "http_status:404"}

    def test_build_ingress_keeps_the_catch_all_last(self):
        existing = [
            {"service": "http_status:404"},
            {"hostname": "a.example.com", "service": "http://127.0.0.1:1"},
        ]
        result = cf.build_ingress(existing, set())
        assert result[-1].get("hostname") is None

    def test_missing_credentials_are_reported_by_name(self, settings, client):
        from fastapi import HTTPException

        from venvy.db.engine import session_scope

        with session_scope() as session, pytest.raises(HTTPException) as exc_info:
            cf.get_credentials(session)
        detail = str(exc_info.value.detail)
        assert "cf_api_token" in detail
        assert "cf_account_id" in detail


class TestProcessEnvironment:
    def _app(self, settings, **overrides) -> App:
        fields = {
            "user_id": 1,
            "name": "demo",
            "slug": "demo",
            "app_dir": str(settings.apps_dir / "demo"),
            "venv_path": str(settings.apps_dir / "demo" / "venv"),
            "entrypoint": "main.py",
        }
        fields.update(overrides)
        return App(**fields)

    def test_exports_both_port_variables(self, settings):
        from venvy.services.processes import build_app_env

        env = build_app_env(self._app(settings, port=8080))
        assert env["PORT"] == "8080"
        assert env["APP_PORT"] == "8080"

    def test_pinned_port_wins_over_the_assigned_one(self, settings):
        from venvy.services.processes import build_app_env

        env = build_app_env(self._app(settings, port=8080, manual_ports="[9000]"))
        assert env["PORT"] == "9000"

    def test_forces_unbuffered_output(self, settings):
        """Otherwise a crashing app's traceback can be lost in a full buffer."""
        from venvy.services.processes import build_app_env

        assert build_app_env(self._app(settings))["PYTHONUNBUFFERED"] == "1"

    def test_points_virtual_env_at_the_app_venv(self, settings):
        from venvy.services.processes import build_app_env

        app = self._app(settings)
        assert build_app_env(app)["VIRTUAL_ENV"] == app.venv_path

    def test_puts_the_venv_first_on_path(self, settings):
        from venvy.services.processes import build_app_env

        app = self._app(settings)
        assert build_app_env(app)["PATH"].startswith(str(app.venv_path))

    @pytest.mark.parametrize(
        "leaked", ["PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV", "PYTHONSTARTUP"]
    )
    def test_strips_inherited_interpreter_and_shell_hooks(self, settings, monkeypatch, leaked):
        """These would leak Venvy's own configuration into the app's environment."""
        from venvy.services.processes import build_app_env

        monkeypatch.setenv(leaked, "/should/not/survive")
        assert leaked not in build_app_env(self._app(settings))

    def test_strips_debugger_hooks(self, settings, monkeypatch):
        from venvy.services.processes import build_app_env

        monkeypatch.setenv("PYDEVD_DISABLE_FILE_VALIDATION", "1")
        monkeypatch.setenv("DEBUGPY_LAUNCHER_PORT", "5678")
        env = build_app_env(self._app(settings))
        assert not any("PYDEVD" in key or "DEBUGPY" in key for key in env)

    def test_applies_the_app_environment_variables(self, settings):
        from venvy.services.processes import build_app_env

        app = self._app(settings, env_json=crypto.encrypt_secret(json.dumps({"MY_KEY": "value"})))
        assert build_app_env(app)["MY_KEY"] == "value"

    def test_survives_corrupted_environment_json(self, settings):
        """A bad row must not make the app unstartable."""
        from venvy.services.processes import build_app_env

        env = build_app_env(self._app(settings, env_json="not json at all"))
        assert env["PYTHONUNBUFFERED"] == "1"


class TestSupervisorPolicy:
    def _app(self, **overrides) -> App:
        fields = {
            "user_id": 1,
            "name": "demo",
            "slug": "demo",
            "app_dir": "/tmp/demo",
            "venv_path": "/tmp/demo/venv",
            "entrypoint": "main.py",
        }
        fields.update(overrides)
        return App(**fields)

    def test_never_policy_blocks_a_restart(self):
        from venvy.services.supervisor import _may_restart

        assert not _may_restart(self._app(restart_policy="never"))

    def test_a_manual_stop_outranks_the_always_policy(self):
        """Stopping an app deliberately must not be undone by its policy."""
        from venvy.services.supervisor import _may_restart

        started = registry.BOOT_TIME + datetime.timedelta(seconds=1)
        app = self._app(
            restart_policy="always",
            last_start_time=started,
            last_manually_stopped_at=started + datetime.timedelta(seconds=1),
        )
        assert not _may_restart(app)

    def test_restarts_an_app_started_this_session(self):
        from venvy.services.supervisor import _may_restart

        app = self._app(
            restart_policy="always",
            last_start_time=registry.BOOT_TIME + datetime.timedelta(seconds=1),
        )
        assert _may_restart(app)

    def test_does_not_resurrect_an_app_from_a_previous_session(self):
        """Only start-on-boot apps come back after Venvy itself restarts."""
        from venvy.services.supervisor import _may_restart

        app = self._app(
            restart_policy="always",
            start_on_boot=False,
            last_start_time=registry.BOOT_TIME - datetime.timedelta(hours=1),
        )
        assert not _may_restart(app)

    def test_start_on_boot_apps_are_restarted_across_sessions(self):
        from venvy.services.supervisor import _may_restart

        app = self._app(
            restart_policy="always",
            start_on_boot=True,
            last_start_time=registry.BOOT_TIME - datetime.timedelta(hours=1),
        )
        assert _may_restart(app)

    def test_handles_naive_timestamps_from_sqlite(self):
        """SQLite hands back naive datetimes; comparing them must not raise."""
        from venvy.services.supervisor import _may_restart

        naive = registry.BOOT_TIME.replace(tzinfo=None) + datetime.timedelta(seconds=1)
        assert _may_restart(self._app(restart_policy="always", last_start_time=naive))


class TestRegistry:
    def test_register_and_unregister_a_pid(self):
        registry.register_pid(1, 4242)
        assert registry.active_pids[1] == 4242
        registry.unregister_pid(1)
        assert 1 not in registry.active_pids

    def test_unregistering_clears_stale_metrics(self):
        registry.register_pid(1, 4242)
        registry.app_metrics[1] = {"cpu": 5.0, "memory": 1024}
        registry.unregister_pid(1)
        assert 1 not in registry.app_metrics

    def test_trigger_is_safe_without_a_supervisor(self):
        """Called from request handlers, which may run before the loop starts."""
        registry.set_supervisor_event(None)
        registry.trigger_supervisor()
