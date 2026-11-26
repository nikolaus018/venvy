"""Preview environments: an app of its own for every pushed branch.

With previews on, a push webhook for a branch other than the app's own
creates (the first time) a copy of the app that follows that branch — its
own folder, user, port and logs; the variables of the app with the preview
overrides on top; environment "preview" — and deploys it. With a preview
domain set, it is served on <branch>.<preview domain> by Venvy's proxy
(point a wildcard DNS record at this server, and Caddy, if used, gets the
certificate). Deleting the branch deletes the preview.
"""
import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from venvy.config import APPS_BASE_DIR
from venvy.crypto import _encrypt_secret, _try_decrypt_secret
from venvy.db import engine
from venvy.models import App, AppAccess

MAX_PREVIEWS = int(__import__("os").getenv("VENVY_MAX_PREVIEWS", "10"))


def branch_label(branch: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")
    return (s or "branch")[:40].strip("-")


def find(parent_id: int, branch: str) -> Optional[App]:
    with Session(engine) as s:
        return s.exec(select(App).where(App.preview_of == parent_id, App.git_branch == branch)).first()


def _merged_env(parent: App) -> str:
    ok, plain = _try_decrypt_secret(parent.env_json)
    env = json.loads(plain or "{}") if ok else {}
    if parent.preview_env:
        ok2, p2 = _try_decrypt_secret(parent.preview_env)
        if ok2:
            env.update(json.loads(p2 or "{}"))
    return _encrypt_secret(json.dumps(env))


async def ensure(parent_id: int, branch: str) -> int:
    """The preview app for *branch* (made if needed); returns its id."""
    from venvy.isolation import _ensure_app_user
    existing = find(parent_id, branch)
    if existing:
        return existing.id
    with Session(engine) as s:
        parent = s.get(App, parent_id)
        if parent is None:
            raise ValueError("App not found.")
        count = len(s.exec(select(App).where(App.preview_of == parent_id)).all())
        if count >= MAX_PREVIEWS:
            raise ValueError(f"This app already has {count} previews (the most allowed).")
        label = branch_label(branch)
        name = f"{parent.name} [{label}]"[:50]
        slug = f"{parent.slug}-preview-{label}"[:80]
        user_dir = APPS_BASE_DIR / str(parent.user_id)
        user_dir.mkdir(exist_ok=True)
        new_dir = user_dir / slug
        if new_dir.exists():
            slug = f"{slug}-{int(time.time())}-{secrets.token_hex(2)}"
            new_dir = user_dir / slug
        new_dir.mkdir(mode=0o750)
        from venvy.groups import _OWN_FIELDS
        fields = {k: v for k, v in parent.model_dump().items()
                  if k not in _OWN_FIELDS and k not in ("app_dir", "venv_path", "env_json")}
        fields.update(name=name, slug=slug, app_dir=str(new_dir), venv_path=str(new_dir / "venv"),
                      env_json=_merged_env(parent), git_url=parent.git_url, git_branch=branch, status="stopped",
                      restart_policy=parent.restart_policy, start_on_boot=False, preview_of=parent_id,
                      environment="preview", previews_enabled=False, auto_deploy=False, instances=1,
                      domain=f"{label}.{parent.preview_domain}" if parent.preview_domain else None,
                      preview_env="", preview_domain=None)
        row = App(**fields)
        s.add(row)
        s.commit()
        s.refresh(row)
        for acc in s.exec(select(AppAccess).where(AppAccess.app_id == parent_id)).all():
            s.add(AppAccess(user_id=acc.user_id, app_id=row.id))
        s.commit()
        new_id = row.id
    await asyncio.to_thread(_ensure_app_user, row)
    from venvy import proxy
    proxy.invalidate()
    return new_id


async def handle_push(parent_id: int, branch: str, deleted: bool) -> dict:
    from venvy import deploy
    from venvy.jobs import _running_tasks
    if deleted:
        p = find(parent_id, branch)
        if p is None:
            return {"preview": None, "deleted": False}

        async def rm() -> None:
            try:
                await remove(p.id)
            except Exception as exc:
                logging.warning("Removing preview %s failed: %s", p.id, exc)
        t = asyncio.ensure_future(rm())
        _running_tasks.add(t)
        t.add_done_callback(_running_tasks.discard)
        return {"preview": p.id, "deleted": True}
    pid = await ensure(parent_id, branch)

    async def go() -> None:
        try:
            await deploy.run_deploy(pid, "webhook", f"preview {branch}", start_if_stopped=True)
        except deploy.DeployError as exc:
            logging.warning("Preview deploy of %s failed to start: %s", branch, exc)
    t = asyncio.ensure_future(go())
    _running_tasks.add(t)
    t.add_done_callback(_running_tasks.discard)
    return {"preview": pid, "deployed": True}


async def remove(app_id: int) -> None:
    """Delete a preview app with everything it has (as the app delete does)."""
    from venvy.api.apps import remove_app_completely
    await remove_app_completely(app_id, "webhook")
