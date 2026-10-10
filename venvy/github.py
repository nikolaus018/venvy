"""GitHub sign-in state and running git on an app's behalf."""
import asyncio
import base64
from pathlib import Path
from typing import Dict, List

from fastapi import HTTPException
from sqlmodel import Session

from venvy.crypto import _decrypt_secret, _encrypt_secret
from venvy.isolation import _app_tool_env, _as_app_user
from venvy.models import App, SystemSetting


# ---------------------------------------------------------------------------
# GitHub integration — device-flow sign-in + per-app git operations
# ---------------------------------------------------------------------------
# Sign-in uses GitHub's OAuth Device Flow: the admin registers a free GitHub
# OAuth App (Client ID only, no secret, no hosted callback) once, then
# "Connect GitHub" shows a short code and opens github.com/login/device in
# the user's own browser tab. We poll GitHub until they approve it there.

_GH_DEVICE_CODE_URL = "https://github.com/login/device/code"


_GH_TOKEN_URL = "https://github.com/login/oauth/access_token"


_GH_API_BASE = "https://api.github.com"


# Pending device-flow authorizations: flow_id -> {device_code, client_id, interval, expires_at}
_gh_device_flows: Dict[str, dict] = {}


def _gh_client_id(session: Session) -> str:
    row = session.get(SystemSetting, "gh_client_id")
    return (row.value if row else "").strip()


def _gh_token(session: Session) -> str:
    row = session.get(SystemSetting, "gh_token")
    return _decrypt_secret(row.value) if row and row.value else ""


def _gh_setting(session: Session, key: str) -> str:
    row = session.get(SystemSetting, key)
    return row.value if row else ""


def _gh_save(session: Session, key: str, value: str) -> None:
    stored = _encrypt_secret(value) if key == "gh_token" else value
    setting = session.get(SystemSetting, key)
    if setting:
        setting.value = stored
    else:
        setting = SystemSetting(key=key, value=stored)
    session.add(setting)


def _git_auth_config(token: str) -> List[str]:
    """-c args that inject the GitHub token as a Basic-auth header scoped to
    github.com only — never touches the stored remote URL or argv."""
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=Authorization: Basic {basic}"]


async def _run_git(app_row: "App", args: List[str], timeout: int = 60, token: str = "") -> str:
    """Run a git command in the app's directory as the app's user; return stdout,
    raise HTTPException with (token-redacted) stderr on failure."""
    env = _app_tool_env(app_row)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    try:
        proc = await asyncio.create_subprocess_exec(
            *_as_app_user(app_row, ["git", "-c", "safe.directory=*", *args]),
            cwd=app_row.app_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        raise HTTPException(500, "git is not installed on the server.")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(500, "git command timed out.")
    if proc.returncode != 0:
        msg = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip() or "git command failed."
        if token:
            msg = msg.replace(token, "***")
        raise HTTPException(400, msg)
    return stdout.decode(errors="replace")


async def _git_status_payload(app_row: "App", session: Session) -> dict:
    app_dir = Path(app_row.app_dir)
    if not await asyncio.to_thread((app_dir / ".git").exists):
        return {"is_repo": False}
    branch = (await _run_git(app_row, ["branch", "--show-current"], timeout=15)).strip()
    try:
        remote_url = (await _run_git(app_row, ["remote", "get-url", "origin"], timeout=15)).strip()
    except HTTPException:
        remote_url = ""
    dirty_out = await _run_git(app_row, ["status", "--porcelain"], timeout=20)
    dirty_count = len([l for l in dirty_out.splitlines() if l.strip()])
    ahead = behind = 0
    if branch:
        try:
            counts = (await _run_git(
                app_row, ["rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"], timeout=15,
            )).strip()
            parts = counts.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
        except HTTPException:
            pass
    try:
        last_commit = (await _run_git(app_row, ["log", "-1", "--format=%h %s (%cr)"], timeout=15)).strip()
    except HTTPException:
        last_commit = ""
    return {
        "is_repo": True,
        "branch": branch,
        "remote_url": remote_url,
        "dirty_count": dirty_count,
        "ahead": ahead,
        "behind": behind,
        "last_commit": last_commit,
    }
