"""Unit tests for the dependency-free core helpers."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from venvy.core import totp
from venvy.core.passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_strength,
    verify_password,
)
from venvy.core.paths import (
    is_protected_name,
    is_safe_path,
    is_safe_upload_name,
    sanitize_slug,
    validate_entrypoint,
)
from venvy.core.timeutils import make_tz_aware, utc_now


class TestPasswords:
    def test_hash_is_salted(self):
        """The same password must not produce the same hash twice."""
        assert hash_password("hunter2pass") != hash_password("hunter2pass")

    def test_roundtrip(self):
        assert verify_password("hunter2pass", hash_password("hunter2pass"))

    def test_rejects_wrong_password(self):
        assert not verify_password("wrong", hash_password("hunter2pass"))

    @pytest.mark.parametrize("bad_hash", ["", "not-base64!!", "YWJj", "x" * 100])
    def test_malformed_hash_fails_closed(self, bad_hash):
        """A corrupted row must return False, never raise."""
        assert verify_password("anything", bad_hash) is False

    def test_accepts_legacy_pbkdf2_hashes(self):
        """Hashes written by older Venvy versions must still verify."""
        import base64
        import hashlib
        import secrets

        salt = secrets.token_bytes(16)
        derived = hashlib.pbkdf2_hmac("sha256", b"legacypass1", salt, 100_000)
        legacy = base64.b64encode(salt + derived).decode()
        assert verify_password("legacypass1", legacy)
        assert not verify_password("otherpass1", legacy)

    @pytest.mark.parametrize("password", ["short1", "alllettersonly", "12345678", ""])
    def test_rejects_weak_passwords(self, password):
        with pytest.raises(PasswordPolicyError):
            validate_password_strength(password)

    def test_accepts_strong_password(self):
        validate_password_strength("goodpass123")


class TestTotp:
    def test_generated_secret_verifies(self):
        secret = totp.generate_secret()
        code = totp._hotp(secret, int(time.time()) // totp.PERIOD_SECONDS)
        assert totp.verify(secret, code)

    def test_rejects_wrong_code(self):
        assert not totp.verify(totp.generate_secret(), "000000")

    @pytest.mark.parametrize("code", ["", "abcdef", "12345", "1234567", "12 34 56"])
    def test_rejects_malformed_codes(self, code):
        assert not totp.verify(totp.generate_secret(), code)

    def test_accepts_clock_skew_within_window(self):
        """A code from the previous step must still be accepted."""
        secret = totp.generate_secret()
        previous = totp._hotp(secret, int(time.time()) // totp.PERIOD_SECONDS - 1)
        assert totp.verify(secret, previous, window=1)

    def test_rejects_codes_beyond_window(self):
        secret = totp.generate_secret()
        far_off = totp._hotp(secret, int(time.time()) // totp.PERIOD_SECONDS - 10)
        assert not totp.verify(secret, far_off, window=1)

    def test_provisioning_uri_is_well_formed(self):
        uri = totp.provisioning_uri("ABCDEFGH", "alice")
        # The label separator is percent-encoded, which the key-uri spec allows
        # and every authenticator app accepts.
        assert uri.startswith("otpauth://totp/Venvy%3Aalice?")
        assert "secret=ABCDEFGH" in uri
        assert "issuer=Venvy" in uri

    def test_rfc6238_test_vector(self):
        """RFC 6238 appendix B: the ASCII secret '12345678901234567890'."""
        import base64

        secret = base64.b32encode(b"12345678901234567890").decode()
        # T = 59s → counter 1, expected 8-digit 94287082; we emit 6 digits.
        assert totp._hotp(secret, 1) == "287082"


class TestPaths:
    def test_allows_paths_inside_root(self, tmp_path: Path):
        assert is_safe_path(tmp_path, tmp_path / "a" / "b.txt")

    def test_allows_root_itself(self, tmp_path: Path):
        assert is_safe_path(tmp_path, tmp_path)

    @pytest.mark.parametrize("escape", ["../outside", "../../etc/passwd", "a/../../.."])
    def test_rejects_traversal(self, tmp_path: Path, escape):
        root = tmp_path / "app"
        root.mkdir()
        assert not is_safe_path(root, (root / escape))

    def test_rejects_symlink_escape(self, tmp_path: Path):
        """A symlink pointing outside the root must not be followed."""
        root = tmp_path / "app"
        root.mkdir()
        outside = tmp_path / "secret.txt"
        outside.write_text("secret")
        link = root / "link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available on this host")
        assert not is_safe_path(root, link)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("My App", "my-app"),
            ("  Spaced  Out  ", "spaced-out"),
            ("weird!!!chars", "weird-chars"),
            ("", "app"),
            ("!!!", "app"),
            # \W is Unicode-aware, so accented letters survive as word chars.
            ("Ünïcödé", "ünïcödé"),
        ],
    )
    def test_sanitize_slug(self, raw, expected):
        assert sanitize_slug(raw) == expected

    def test_slug_never_empty(self):
        assert sanitize_slug("---") == "app"

    @pytest.mark.parametrize("name", ["main.py", "data file.csv", "a-b_c.tar.gz", "x(1).txt"])
    def test_accepts_safe_upload_names(self, name):
        assert is_safe_upload_name(name)

    @pytest.mark.parametrize(
        "name", ["", ".", "..", ".hidden", ".jailrc", "a/b", "a\\b", "nul\0byte", "we|rd"]
    )
    def test_rejects_unsafe_upload_names(self, name):
        assert not is_safe_upload_name(name)

    def test_protected_names(self):
        assert is_protected_name(".jailrc")
        assert is_protected_name(".bashrc-venvy")
        assert not is_protected_name("main.py")

    @pytest.mark.parametrize("entrypoint", ["main.py", "app-1.py", "server.rs"])
    def test_accepts_valid_entrypoints(self, entrypoint):
        assert validate_entrypoint(entrypoint) == entrypoint

    def test_blank_entrypoint_allowed(self):
        assert validate_entrypoint("  ") == ""

    @pytest.mark.parametrize(
        "entrypoint", ["src/main.py", "../main.py", "/etc/passwd", "main;rm -rf.py", "a b.py"]
    )
    def test_rejects_path_like_entrypoints(self, entrypoint):
        with pytest.raises(ValueError):
            validate_entrypoint(entrypoint)


class TestTimeUtils:
    def test_utc_now_is_aware(self):
        assert utc_now().tzinfo is not None

    def test_make_tz_aware_adds_utc(self):
        import datetime

        naive = datetime.datetime(2026, 1, 1, 12, 0)
        assert make_tz_aware(naive).tzinfo == datetime.UTC

    def test_make_tz_aware_leaves_aware_values_alone(self):
        aware = utc_now()
        assert make_tz_aware(aware) is aware
