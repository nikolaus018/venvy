"""GitHub sign-in (device flow) and per-app git routes."""
import aiohttp
import asyncio
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from venvy.audit import audit_log
from venvy.auth import (
    check_admin,
    check_ip_allowlist,
    get_current_user,
    rate_limit_mutations,
    require_operator,
)
from venvy.db import get_session
from venvy.github import (
    _GH_API_BASE,
    _gh_client_id,
    _GH_DEVICE_CODE_URL,
    _gh_device_flows,
    _gh_save,
    _gh_setting,
    _gh_token,
    _GH_TOKEN_URL,
    _git_auth_config,
    _git_status_payload,
    _run_git,
)
from venvy.isolation import _chown_app_dir, _ensure_app_user
from venvy.jobs import _start_job
from venvy.models import App, SystemSetting, User

router = APIRouter(
    prefix="/api", tags=["apps"], dependencies=[Depends(check_ip_allowlist)]
)


class GhClientIdReq(BaseModel):
    client_id: str


@router.put("/system/github/client-id")
def set_gh_client_id(
    req: GhClientIdReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    _gh_save(session, "gh_client_id", req.client_id.strip())
    session.commit()
    audit_log(admin.username, "system_setting_change", "key=gh_client_id")
    return {"ok": True}


@router.post("/system/github/device/start")
async def gh_device_start(
    _: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Start a GitHub OAuth Device Flow authorization. The client polls
    /system/github/device/poll with the returned flow_id until it completes."""
    client_id = _gh_client_id(session)
    if not client_id:
        raise HTTPException(400, "Set a GitHub OAuth App Client ID first.")

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                _GH_DEVICE_CODE_URL,
                data={"client_id": client_id, "scope": "repo"},
                headers={"Accept": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Could not reach GitHub: {exc}")

    if "device_code" not in data:
        raise HTTPException(
            400,
            data.get("error_description") or data.get("error") or "GitHub rejected the request — check the Client ID.",
        )

    now = time.time()
    for fid in [f for f, v in _gh_device_flows.items() if now > v["expires_at"]]:
        _gh_device_flows.pop(fid, None)

    flow_id = secrets.token_hex(16)
    _gh_device_flows[flow_id] = {
        "device_code": data["device_code"],
        "client_id": client_id,
        "interval": data.get("interval", 5),
        "expires_at": now + data.get("expires_in", 900),
    }
    return {
        "flow_id": flow_id,
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri") or "https://github.com/login/device",
        "expires_in": data.get("expires_in", 900),
        "interval": data.get("interval", 5),
    }


class GhDevicePollReq(BaseModel):
    flow_id: str


@router.post("/system/github/device/poll")
async def gh_device_poll(
    req: GhDevicePollReq,
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    """Poll GitHub for whether the user has approved the device authorization."""
    flow = _gh_device_flows.get(req.flow_id)
    if not flow:
        raise HTTPException(404, "Sign-in expired — start again.")
    if time.time() > flow["expires_at"]:
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, "Sign-in code expired — start again.")

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(
                _GH_TOKEN_URL,
                data={
                    "client_id": flow["client_id"],
                    "device_code": flow["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Could not reach GitHub: {exc}")

    error = data.get("error")
    if error == "authorization_pending":
        return {"status": "pending"}
    if error == "slow_down":
        flow["interval"] = data.get("interval", flow["interval"] + 5)
        return {"status": "pending", "interval": flow["interval"]}
    if error == "expired_token":
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, "Sign-in code expired — start again.")
    if error == "access_denied":
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, "Sign-in was cancelled.")
    if error:
        _gh_device_flows.pop(req.flow_id, None)
        raise HTTPException(400, data.get("error_description") or error)

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(502, "GitHub did not return an access token.")
    _gh_device_flows.pop(req.flow_id, None)

    gh_user: dict = {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(
                f"{_GH_API_BASE}/user",
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
            ) as resp:
                gh_user = await resp.json(content_type=None)
    except aiohttp.ClientError:
        pass

    username = gh_user.get("login", "")
    avatar = gh_user.get("avatar_url", "")

    _gh_save(session, "gh_token", access_token)
    _gh_save(session, "gh_username", username)
    _gh_save(session, "gh_avatar", avatar)
    session.commit()
    audit_log(admin.username, "github_connect", f"connected as {username}")
    return {"status": "complete", "username": username, "avatar_url": avatar}


@router.get("/system/github/status")
def gh_status(
    _: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return {
        "connected": bool(_gh_token(session)),
        "client_id_set": bool(_gh_client_id(session)),
        "username": _gh_setting(session, "gh_username"),
        "avatar_url": _gh_setting(session, "gh_avatar"),
    }


@router.get("/system/github/repos")
async def gh_list_repos(
    _: User = Depends(require_operator),
    session: Session = Depends(get_session),
):
    """List repos the connected GitHub account can push to, newest-updated first."""
    token = _gh_token(session)
    if not token:
        raise HTTPException(400, "Not signed in to GitHub.")

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    timeout = aiohttp.ClientTimeout(total=15)
    repos: List[Dict] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            for page in (1, 2, 3):
                url = f"{_GH_API_BASE}/user/repos?per_page=100&page={page}&sort=updated&affiliation=owner,collaborator,organization_member"
                async with http.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        break
                    batch = await resp.json(content_type=None)
                if not isinstance(batch, list) or not batch:
                    break
                for r in batch:
                    repos.append({
                        "full_name": r.get("full_name", ""),
                        "clone_url": r.get("clone_url", ""),
                        "private": bool(r.get("private")),
                        "default_branch": r.get("default_branch") or "main",
                    })
                if len(batch) < 100:
                    break
    except aiohttp.ClientError as exc:
        raise HTTPException(502, f"Could not reach GitHub: {exc}")

    return {"repos": repos}


@router.post("/system/github/disconnect")
def gh_disconnect(
    admin: User = Depends(check_admin),
    session: Session = Depends(get_session),
):
    for key in ("gh_token", "gh_username", "gh_avatar"):
        setting = session.get(SystemSetting, key)
        if setting:
            session.delete(setting)
    session.commit()
    audit_log(admin.username, "github_disconnect", "")
    return {"ok": True}


@router.get("/apps/{app_id}/git/status")
async def git_status(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    return await _git_status_payload(app_row, session)


class GitCloneReq(BaseModel):
    url: str
    branch: Optional[str] = None


@router.post("/apps/{app_id}/git/clone")
async def git_clone(
    app_id: int,
    req: GitCloneReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Connect an app to a repository: init + fetch + checkout into its existing
    directory. Safe — git refuses (and we roll back) if any tracked file would
    overwrite something already there (e.g. logs/, venv/)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    app_dir = Path(app_row.app_dir)
    if await asyncio.to_thread((app_dir / ".git").exists):
        raise HTTPException(400, "This app is already connected to a repository.")

    url = req.url.strip()
    if not re.match(r"^https://[^\s@]+\.[^\s@]+/", url):
        raise HTTPException(400, "Only https:// repository URLs are supported.")
    branch = (req.branch or "").strip()
    if branch and not re.fullmatch(r"[A-Za-z0-9_./\-]+", branch):
        raise HTTPException(400, "Invalid branch name.")

    token = _gh_token(session)
    await asyncio.to_thread(_ensure_app_user, app_row)

    async def _job() -> dict:
        auth_cfg = _git_auth_config(token)
        target_branch = branch
        if not target_branch:
            out = await _run_git(app_row, [*auth_cfg, "ls-remote", "--symref", url, "HEAD"], timeout=30, token=token)
            m = re.search(r"ref:\s*refs/heads/(\S+)\s+HEAD", out)
            target_branch = m.group(1) if m else "main"

        await _run_git(app_row, ["init", "-q"], token=token)
        try:
            await _run_git(app_row, ["remote", "add", "origin", url], token=token)
            await _run_git(app_row, [*auth_cfg, "fetch", "--depth", "1", "origin", target_branch], timeout=180, token=token)
            await _run_git(app_row, ["checkout", "-b", target_branch, "--track", f"origin/{target_branch}"], token=token)
        except HTTPException:
            await asyncio.to_thread(shutil.rmtree, app_dir / ".git", ignore_errors=True)
            raise
        await asyncio.to_thread(_chown_app_dir, app_row)
        audit_log(user.username, "git_clone", f"app '{app_row.name}' (id={app_id}) <- {url}")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_clone", _job)


@router.post("/apps/{app_id}/git/fetch")
async def git_fetch(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    token = _gh_token(session)

    async def _job() -> dict:
        await _run_git(app_row, [*_git_auth_config(token), "fetch", "origin"], timeout=180, token=token)
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_fetch", _job)


@router.post("/apps/{app_id}/git/pull")
async def git_pull(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    token = _gh_token(session)

    async def _job() -> dict:
        await _run_git(app_row, [*_git_auth_config(token), "pull", "--ff-only", "origin"], timeout=180, token=token)
        await asyncio.to_thread(_chown_app_dir, app_row)
        audit_log(user.username, "git_pull", f"app '{app_row.name}' (id={app_id})")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_pull", _job)


class GitPushReq(BaseModel):
    force: bool = False


@router.post("/apps/{app_id}/git/push")
async def git_push(
    app_id: int,
    req: GitPushReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    token = _gh_token(session)

    async def _job() -> dict:
        args = [*_git_auth_config(token), "push"]
        if req.force:
            args.append("--force-with-lease")
        args.append("origin")
        await _run_git(app_row, args, timeout=180, token=token)
        audit_log(user.username, "git_push", f"app '{app_row.name}' (id={app_id}) force={req.force}")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_push", _job)


class GitCommitReq(BaseModel):
    message: str


@router.post("/apps/{app_id}/git/commit")
async def git_commit(
    app_id: int,
    req: GitCommitReq,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    if not await asyncio.to_thread((Path(app_row.app_dir) / ".git").exists):
        raise HTTPException(400, "Not a git repository.")
    message = req.message.strip()
    if not message:
        raise HTTPException(400, "Commit message is required.")

    gh_username = _gh_setting(session, "gh_username")
    name = gh_username or user.username
    email = f"{gh_username}@users.noreply.github.com" if gh_username else f"{user.username}@venvy.local"

    async def _job() -> dict:
        await _run_git(app_row, ["add", "-A"], timeout=60)
        try:
            await _run_git(
                app_row,
                ["-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-m", message],
                timeout=30,
            )
        except HTTPException as exc:
            if "nothing to commit" in str(exc.detail).lower():
                raise HTTPException(400, "Nothing to commit — working tree is clean.")
            raise
        audit_log(user.username, "git_commit", f"app '{app_row.name}' (id={app_id}): {message[:80]}")
        return await _git_status_payload(app_row, session)

    return _start_job(user, "git_commit", _job)


@router.delete("/apps/{app_id}/git")
async def git_disconnect(
    app_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rl: None = Depends(rate_limit_mutations),
):
    """Unlink the repo (removes .git only — the working files are left in place)."""
    app_row = session.get(App, app_id)
    if not app_row:
        raise HTTPException(404)
    git_dir = Path(app_row.app_dir) / ".git"
    if not await asyncio.to_thread(git_dir.exists):
        raise HTTPException(400, "Not a git repository.")
    await asyncio.to_thread(shutil.rmtree, git_dir, ignore_errors=True)
    audit_log(user.username, "git_disconnect", f"app '{app_row.name}' (id={app_id})")
    return {"ok": True}
