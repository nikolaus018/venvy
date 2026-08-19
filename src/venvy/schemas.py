"""Pydantic request and response models for the HTTP API.

Validation that depends only on the value itself lives here as a field
constraint or validator, so a malformed request is rejected before any handler
runs. Validation that needs database state (does this hostname belong to
another app?) stays in the route.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from venvy.core.paths import validate_entrypoint

__all__ = [
    "AppUpdateRequest",
    "BatchSettingItem",
    "CfPublishRequest",
    "FileOperationRequest",
    "PackageRequest",
    "PasswordChangeRequest",
    "SystemSettingRequest",
    "TotpConfirmRequest",
    "TotpDisableRequest",
    "UserCreateRequest",
    "UserLoginRequest",
]

Runtime = Literal["python", "rust"]
RestartPolicy = Literal["never", "on-failure", "always"]
Role = Literal["admin", "operator", "viewer"]
UrlSourcePreference = Literal["local", "public"]

#: Port 0 is accepted by the update endpoint to mean "clear this port".
Port = Annotated[int, Field(ge=1, le=65535)]


# ── Authentication ───────────────────────────────────────────────────────────


class UserLoginRequest(BaseModel):
    """Credentials submitted to `POST /api/auth/login`."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    #: Supplied on the second step when the account has TOTP enabled.
    totp_code: str | None = None


class UserCreateRequest(BaseModel):
    """A new account, for first-run setup or admin user creation."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)
    #: Ignored during first-run setup, which always creates an admin.
    role: Role | None = None

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Username cannot be blank.")
        return cleaned


class PasswordChangeRequest(BaseModel):
    """Self-service password change."""

    current_password: str
    password: str
    password_confirm: str


class TotpConfirmRequest(BaseModel):
    """The code proving the user's authenticator is set up correctly."""

    totp_code: str


class TotpDisableRequest(BaseModel):
    """Password confirmation required to turn two-factor auth off."""

    password: str


# ── Applications ─────────────────────────────────────────────────────────────


class AppUpdateRequest(BaseModel):
    """Partial update of an app. Every field is optional; omitted means unchanged."""

    name: str | None = Field(default=None, max_length=50)
    start_on_boot: bool | None = None
    restart_policy: RestartPolicy | None = None
    runtime: Runtime | None = None
    #: 0 clears the port rather than setting it.
    port: int | None = Field(default=None, ge=0, le=65535)
    manual_ports: list[Port] | None = None
    env_vars: dict[str, str] | None = None
    entrypoint: str | None = None
    url_source_pref: UrlSourcePreference | None = None
    #: 0 means unlimited. The upper bound is 1 TiB, which is a typo guard
    #: rather than a real ceiling.
    memory_limit_mb: int | None = Field(default=None, ge=0, le=1_048_576)
    #: 0 means unlimited. Values above 100 are legitimate on multi-core hosts —
    #: 200 means "two cores' worth".
    cpu_limit_percent: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("App name cannot be empty.")
        return cleaned

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_entrypoint(value)


# ── Files and packages ───────────────────────────────────────────────────────


class FileOperationRequest(BaseModel):
    """One filesystem operation inside an app's directory."""

    operation: Literal["create_file", "create_folder", "delete", "rename", "copy", "save"]
    path: str
    #: Destination for `rename` and `copy`.
    new_path: str | None = None
    #: Contents for `save`.
    content: str | None = None


class PackageRequest(BaseModel):
    """A package to install into an app's environment."""

    name: str = Field(min_length=1, max_length=214)


# ── System settings ──────────────────────────────────────────────────────────


class SystemSettingRequest(BaseModel):
    """A single setting value."""

    value: str


class BatchSettingItem(BaseModel):
    """One entry in a batched settings update."""

    key: str = Field(min_length=1, max_length=128)
    value: str


# ── Cloudflare ───────────────────────────────────────────────────────────────


class CfPublishRequest(BaseModel):
    """Publish one of an app's ports at a public hostname."""

    hostname: str = Field(min_length=3, max_length=253)
    local_port: Port
