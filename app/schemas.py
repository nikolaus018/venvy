from typing import Dict, List, Optional

from pydantic import BaseModel


class AppUpdateReq(BaseModel):
    name: Optional[str] = None
    start_on_boot: Optional[bool] = None
    restart_policy: Optional[str] = None
    runtime: Optional[str] = None
    port: Optional[int] = None
    manual_ports: Optional[List[int]] = None
    env_vars: Optional[Dict[str, str]] = None
    entrypoint: Optional[str] = None
    url_source_pref: Optional[str] = None


class SystemSettingReq(BaseModel):
    value: str


class FileOpReq(BaseModel):
    operation: str
    path: str
    new_path: Optional[str] = None
    content: Optional[str] = None


class PackageReq(BaseModel):
    name: str


class UserReq(BaseModel):
    username: str
    password: str


class PwdReq(BaseModel):
    current_password: str
    password: str
    password_confirm: str


class TotpSetupReq(BaseModel):
    totp_code: str


class TotpDisableReq(BaseModel):
    password: str
