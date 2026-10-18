"""Deploying an app: pull its code from git, install dependencies, build,
restart, check that it came up — and go back to what ran before if it did not.

Everything that touches the app's files (git, the build command, tar) runs as
the app's own user (see isolation.py), never as Venvy: an app owns its
directory, and a hook or config in it must not run with more rights than the
app has.
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import signal
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlmodel import Session, select

from venvy import monitor, notify, runtimes
from venvy.audit import audit_log
from venvy.config import DATA_DIR, utc_now
from venvy.crypto import _decrypt_secret, _encrypt_secret
from venvy.db import engine
from venvy.isolation import _app_ids, _app_tool_env, _as_app_user, _ensure_app_user, _chown_app_dir
from venvy.models import App, Release

RELEASES_DIR = DATA_DIR / "releases"
DEPLOY_LOG_DIR = DATA_DIR / "deploy-logs"
DEPLOY_KEY_DIR = DATA_DIR / "deploy-keys"
KEEP_RELEASES = int(os.getenv("VENVY_KEEP_RELEASES", "10"))
SNAPSHOT_MAX_BYTES = 2 * 1024 ** 3
HEALTH_WAIT_SEC = int(os.getenv("VENVY_HEALTH_WAIT_SEC", "60"))     # tests shorten it

# Not part of a snapshot: rebuilt from the manifest, or logs and caches.
SNAPSHOT_EXCLUDES = ("./venv", "./node_modules", "./target", "./vendor/bundle", "./logs", "./.git",
                     "./.cache", "*/__pycache__", "*.pyc")

_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,100}")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_URL_RE = re.compile(r"(https://[A-Za-z0-9.\-]+(:\d+)?/\S+|ssh://\S+|[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:\S+)")
# A repository on this same machine (a bare repo you push to over ssh, say).
# Off by default: a local path lets the app's user read any repository it can reach.
ALLOW_LOCAL_GIT = os.getenv("VENVY_ALLOW_LOCAL_GIT", "0") == "1"


class DeployError(Exception):
    """A deploy step failed; the message is meant for the user."""


# ---------------------------------------------------------------------------
# Settings checks
# ---------------------------------------------------------------------------

def clean_git_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("-") or any(c in u for c in " \t\r\n\0'\"`$;|&<>\\"):
        raise ValueError("The repository must be an https:// or ssh:// URL, or user@host:path.")
    if ALLOW_LOCAL_GIT and (u.startswith("/") or u.startswith("file:///")):
        return u
    if not _URL_RE.fullmatch(u):
        raise ValueError("The repository must be an https:// or ssh:// URL, or user@host:path.")
    return u


def clean_branch(branch: str) -> str:
    b = (branch or "").strip() or "main"
    if not _BRANCH_RE.fullmatch(b) or ".." in b or b.endswith("/") or b.endswith(".lock"):
        raise ValueError("That is not a valid branch name.")
    return b


def clean_build_command(cmd: Optional[str]) -> Optional[str]:
    c = (cmd or "").strip()
    if not c:
        return None
    if len(c) > 2000 or "\0" in c:
        raise ValueError("The build command is too long.")
    return c


def new_webhook_secret() -> str:
    import secrets
    return secrets.token_urlsafe(24)


def is_ssh_url(url: str) -> bool:
    if url.startswith("/") or url.startswith("file://"):
        return False
    return url.startswith("ssh://") or (not url.startswith("https://") and "@" in url.split(":")[0])


# ---------------------------------------------------------------------------
# State and logs
# ---------------------------------------------------------------------------
_locks: Dict[int, asyncio.Lock] = {}
_state: Dict[int, dict] = {}     # app_id -> {"running", "status", "log", "started", "finished", "trigger"}


def _lock(app_id: int) -> asyncio.Lock:
    return _locks.setdefault(app_id, asyncio.Lock())


def is_running(app_id: int) -> bool:
    return _lock(app_id).locked()


def state(app_id: int) -> dict:
    st = _state.get(app_id)
    if st:
        return {**st, "log": st["log"][-400:]}
    f = DEPLOY_LOG_DIR / f"{app_id}.log"
    log = []
    with contextlib.suppress(OSError):
        log = f.read_text(errors="replace").splitlines()[-400:]
    return {"running": False, "status": None, "log": log, "started": None, "finished": None, "trigger": None}


class _Log:
    def __init__(self, app_id: int, secrets: List[str]):
        self.app_id = app_id
        self.secrets = [s for s in secrets if s]
        self.lines: List[str] = []
        DEPLOY_LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = DEPLOY_LOG_DIR / f"{app_id}.log"
        self.fh = open(self.path, "w", encoding="utf-8")

    def __call__(self, line: str) -> None:
        for s in self.secrets:
            line = line.replace(s, "***")
        self.lines.append(line)
        if self.app_id in _state:
            _state[self.app_id]["log"] = self.lines
        with contextlib.suppress(OSError):
            self.fh.write(line + "\n")
            self.fh.flush()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.fh.close()


# ---------------------------------------------------------------------------
# Running commands as the app
# ---------------------------------------------------------------------------

def _app_env_for_commands(app: App) -> Dict[str, str]:
    from venvy.process import _build_app_env
    env = _build_app_env(app)
    if _app_ids(app):
        from venvy.isolation import _app_username
        env.update(HOME=app.app_dir, USER=_app_username(app.id), LOGNAME=_app_username(app.id))
    return env


def _git_env(app: App, log: _Log) -> Dict[str, str]:
    env = _app_tool_env(app)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh" + (":file" if ALLOW_LOCAL_GIT else "")   # never ext::
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    url = app.git_url or ""
    auth = {}
    if app.git_auth:
        with contextlib.suppress(ValueError):
            auth = json.loads(_decrypt_secret(app.git_auth) or "{}")
    if url.startswith("https://") and auth.get("token"):
        host = url.split("/")[2]
        basic = base64.b64encode(f"x-access-token:{auth['token']}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.https://{host}/.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
        log.secrets += [auth["token"], basic]
    elif is_ssh_url(url):
        key = DEPLOY_KEY_DIR / str(app.id) / "id_ed25519"
        known = DEPLOY_KEY_DIR / str(app.id) / "known_hosts"
        parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", f"UserKnownHostsFile={known}"]
        if key.exists():
            parts += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
        env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(p) for p in parts)
    return env


async def _run(app: App, argv: List[str], log: _Log, env: Dict[str, str], cwd: str,
               timeout: int, what: str) -> str:
    """Run argv as the app's user, logging its output; returns it. Raises DeployError."""
    log(f"$ {what}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *_as_app_user(app, argv),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env, cwd=cwd, start_new_session=True)
    except FileNotFoundError:
        raise DeployError(f"{argv[0]} is not installed on this host.")
    out: List[str] = []

    async def pump() -> None:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            out.append(line)
            log("  " + line)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        raise DeployError(f"{what} timed out after {timeout} seconds.")
    if proc.returncode != 0:
        raise DeployError(f"{what} failed (exit code {proc.returncode}).")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Deploy keys
# ---------------------------------------------------------------------------

async def generate_deploy_key(app: App) -> str:
    """Make (or replace) the app's SSH deploy key; returns the public half."""
    d = DEPLOY_KEY_DIR / str(app.id)
    DEPLOY_KEY_DIR.mkdir(mode=0o711, parents=True, exist_ok=True)
    d.mkdir(mode=0o700, exist_ok=True)
    for name in ("id_ed25519", "id_ed25519.pub"):
        (d / name).unlink(missing_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"venvy-app-{app.id}", "-f", str(d / "id_ed25519"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise DeployError(f"ssh-keygen failed: {out.decode(errors='replace')[:300]}")
    ids = await asyncio.to_thread(_ensure_app_user, app)
    if ids:
        for p in (d, d / "id_ed25519", d / "id_ed25519.pub"):
            os.chown(p, ids[0], ids[1])
    return (d / "id_ed25519.pub").read_text().strip()


def public_key(app_id: int) -> Optional[str]:
    with contextlib.suppress(OSError):
        return (DEPLOY_KEY_DIR / str(app_id) / "id_ed25519.pub").read_text().strip()
    return None


def forget_keys(app_id: int) -> None:
    import shutil
    shutil.rmtree(DEPLOY_KEY_DIR / str(app_id), ignore_errors=True)
    shutil.rmtree(RELEASES_DIR / str(app_id), ignore_errors=True)
    (DEPLOY_LOG_DIR / f"{app_id}.log").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

def _is_repo(app: App) -> bool:
    return (Path(app.app_dir) / ".git").exists()


async def _git(app: App, log: _Log, args: List[str], timeout: int = 600) -> str:
    return await _run(app, ["git", *args], log, _git_env(app, log), app.app_dir, timeout, "git " + " ".join(args))


async def current_commit(app: App) -> Optional[Tuple[str, str]]:
    """(sha, subject) of HEAD, or None if the app is not a git checkout."""
    if not _is_repo(app):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_as_app_user(app, ["git", "log", "-1", "--format=%H%n%s"]),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=_app_tool_env(app), cwd=app.app_dir)
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    lines = out.decode(errors="replace").splitlines()
    return (lines[0], lines[1] if len(lines) > 1 else "") if lines else None


async def _checkout_remote(app: App, log: _Log) -> None:
    if not _is_repo(app):
        await _git(app, log, ["init", "-q"])
    branch = app.git_branch
    if await _has_remote(app):
        await _git(app, log, ["remote", "remove", "origin"])
    await _git(app, log, ["remote", "add", "origin", app.git_url or ""])
    await _git(app, log, ["fetch", "--prune", "--tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"])
    await _git(app, log, ["checkout", "-f", "-B", branch, f"refs/remotes/origin/{branch}"])


async def _has_remote(app: App) -> bool:
    proc = await asyncio.create_subprocess_exec(
        *_as_app_user(app, ["git", "remote"]), stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=_app_tool_env(app), cwd=app.app_dir)
    out, _ = await proc.communicate()
    return "origin" in out.decode().split()


# ---------------------------------------------------------------------------
# Snapshots (apps that are not a git checkout)
# ---------------------------------------------------------------------------

async def _snapshot(app: App, log: _Log, dest_dir: Optional[Path] = None) -> Tuple[str, int]:
    """Archive the app's files; returns (file name, bytes)."""
    d = dest_dir or RELEASES_DIR / str(app.id)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = f"{utc_now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.tar.gz"
    dest = d / name
    argv = ["tar", "-czf", "-", "-C", app.app_dir] + [f"--exclude={e}" for e in SNAPSHOT_EXCLUDES] + ["."]
    log("Taking a snapshot of the app's files…")
    proc = await asyncio.create_subprocess_exec(
        *_as_app_user(app, argv), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=_app_tool_env(app))
    size = 0
    with open(dest, "wb") as f:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > SNAPSHOT_MAX_BYTES:
                proc.kill()
                dest.unlink(missing_ok=True)
                raise DeployError("The app's files are too large to snapshot (over 2 GB).")
            f.write(chunk)
    _, err = await proc.communicate()
    if proc.returncode not in (0, 1):            # 1: a file changed while being read
        dest.unlink(missing_ok=True)
        raise DeployError(f"tar failed: {err.decode(errors='replace')[:300]}")
    os.chmod(dest, 0o600)
    return name, size


async def _restore_snapshot(app: App, archive: str, log: _Log, src_dir: Optional[Path] = None) -> None:
    path = (src_dir or RELEASES_DIR / str(app.id)) / archive
    if not path.is_file() or Path(archive).name != archive:
        raise DeployError("That snapshot is gone.")
    log("Restoring the snapshot…")
    proc = await asyncio.create_subprocess_exec(
        *_as_app_user(app, ["tar", "-xzf", "-", "-C", app.app_dir, "--no-same-owner"]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        env=_app_tool_env(app))
    assert proc.stdin is not None
    with open(path, "rb") as f:
        while True:
            chunk = await asyncio.to_thread(f.read, 1 << 20)
            if not chunk:
                break
            try:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                break
    with contextlib.suppress(Exception):
        proc.stdin.close()
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise DeployError(f"tar failed: {err.decode(errors='replace')[:300]}")


def _prune_releases(app_id: int) -> None:
    with Session(engine) as s:
        rows = s.exec(select(Release).where(Release.app_id == app_id).order_by(Release.id.desc())).all()
        for r in rows[KEEP_RELEASES:]:
            if r.archive:
                (RELEASES_DIR / str(app_id) / r.archive).unlink(missing_ok=True)
            s.delete(r)
        s.commit()


def list_releases(app_id: int) -> List[dict]:
    with Session(engine) as s:
        rows = s.exec(select(Release).where(Release.app_id == app_id).order_by(Release.id.desc())).all()
    return [{"id": r.id, "created_at": r.created_at.isoformat(), "kind": r.kind, "commit": r.commit,
             "message": r.message, "size": r.size, "by": r.by, "restorable": bool(r.commit or r.archive)} for r in rows]


# ---------------------------------------------------------------------------
# The deploy itself
# ---------------------------------------------------------------------------

def _app(app_id: int) -> Optional[App]:
    with Session(engine) as s:
        return s.get(App, app_id)


def _record(app_id: int, **fields) -> None:
    with Session(engine) as s:
        row = s.get(App, app_id)
        if row:
            for k, v in fields.items():
                setattr(row, k, v)
            s.add(row)
            s.commit()


def _add_release(app_id: int, kind: str, commit: Optional[str], message: str, archive: Optional[str],
                 size: int, by: str) -> None:
    with Session(engine) as s:
        s.add(Release(app_id=app_id, kind=kind, commit=commit, message=message[:300], archive=archive, size=size, by=by))
        s.commit()
    _prune_releases(app_id)


async def _install_and_build(app: App, log: _Log, build: bool = True) -> None:
    """Dependencies from the manifest, then the build command."""
    env = _app_tool_env(app)
    app_dir = Path(app.app_dir)
    steps = await asyncio.to_thread(runtimes.sync_steps, app.runtime, app_dir)
    if app.runtime == "python":
        pip = Path(app.venv_path) / "bin" / "pip"
        req = app_dir / "requirements.txt"
        if req.is_file() and pip.exists():
            await _run(app, [str(pip), "install", "--no-input", "-r", str(req)], log, env, app.app_dir, 900, "pip install -r requirements.txt")
    for step in steps:
        await _run(app, step.argv, log, env, app.app_dir if step.in_app_dir else "/", step.timeout, step.what)
    if build and app.build_command:
        await _run(app, ["sh", "-c", app.build_command], log, _app_env_for_commands(app), app.app_dir, 1800,
                   f"build: {app.build_command}")


async def _restart(app_id: int, log: _Log, start_if_stopped: bool) -> None:
    from venvy.process import restart_app_process_logic, start_app_process_logic
    app = _app(app_id)
    assert app
    if app.status in ("running", "starting"):
        log("Restarting the app…")
        await restart_app_process_logic(app_id)
    elif start_if_stopped:
        log("Starting the app…")
        with Session(engine) as s:
            await start_app_process_logic(app_id, s)


async def _wait_healthy(app_id: int, log: _Log) -> bool:
    """True once the restarted app looks alive: its health check passes, or — with
    no health check — it is still running a few seconds after the start."""
    deadline = time.monotonic() + HEALTH_WAIT_SEC
    await asyncio.sleep(3)
    app = _app(app_id)
    if not app:
        return False
    checking = bool(app.healthcheck_path)
    if checking:
        log(f"Waiting for {app.healthcheck_path} to answer…")
    else:
        await asyncio.sleep(3)
    while time.monotonic() < deadline:
        app = _app(app_id)
        if not app or app.status == "error":
            return False
        if app.status == "running":
            if not checking:
                return True
            if app.port and (await monitor.probe(app.port, app.healthcheck_path))["ok"]:
                return True
        elif app.status == "stopped":
            return False
        await asyncio.sleep(2)
    return False


async def run_deploy(app_id: int, by: str, trigger: str = "manual", target: Optional[Release] = None,
                     start_if_stopped: bool = False) -> dict:
    """Deploy (or, with *target*, roll back to) an app. Raises DeployError if it cannot start,
    returns {"status": "ok" | "failed" | "rolled-back", "commit": ...} otherwise."""
    lock = _lock(app_id)
    if lock.locked():
        raise DeployError("A deploy is already running for this app.")
    async with lock:
        app = _app(app_id)
        if not app:
            raise DeployError("App not found.")
        if target is None and not app.git_url and not app.build_command:
            raise DeployError("Set a git repository or a build command first.")
        _state[app_id] = {"running": True, "status": "running", "log": [], "started": utc_now().isoformat(),
                          "finished": None, "trigger": trigger}
        log = _Log(app_id, [])
        _state[app_id]["log"] = log.lines
        _record(app_id, last_deploy_status="running", last_deploy_at=utc_now())
        what = "Rolling back" if target else "Deploying"
        log(f"{what} {app.name} ({trigger}, by {by})")
        status, commit_sha, message = "failed", None, ""
        try:
            await asyncio.to_thread(_ensure_app_user, app)
            was_running = app.status in ("running", "starting")
            before = await current_commit(app)
            before_archive: Optional[Tuple[str, int]] = None
            if not (before or app.git_url):
                before_archive = await _snapshot(app, log)
            # What to go back to if this goes wrong.
            if before or before_archive:
                _add_release(app_id, "before-deploy", before[0] if before else None, before[1] if before else "",
                             before_archive[0] if before_archive else None, before_archive[1] if before_archive else 0, by)

            async def go_back() -> None:
                log("Putting the previous version back…")
                if before:
                    await _git(app, log, ["checkout", "-f", "-B", app.git_branch, before[0]])
                elif before_archive:
                    await _restore_snapshot(app, before_archive[0], log)

            try:
                if target is not None:
                    if target.commit:
                        if not _is_repo(app):
                            raise DeployError("This release is a git commit, but the app has no git checkout.")
                        await _git(app, log, ["checkout", "-f", "-B", app.git_branch, target.commit])
                    elif target.archive:
                        await _restore_snapshot(app, target.archive, log)
                    else:
                        raise DeployError("That release cannot be restored.")
                elif app.git_url:
                    await _checkout_remote(app, log)
                await _chown_if_needed(app)
                await _install_and_build(app, log)
            except Exception:
                if target is None and (before or before_archive):
                    with contextlib.suppress(Exception):
                        await go_back()
                raise

            now = await current_commit(app)
            if now:
                commit_sha, message = now
                log(f"Now at {commit_sha[:10]} — {message}")
            await _restart(app_id, log, start_if_stopped)
            fresh = _app(app_id)
            should_run = was_running or start_if_stopped or (fresh and fresh.status in ("running", "starting"))
            if should_run and not await _wait_healthy(app_id, log):
                log("The app did not come up healthy.")
                if target is None and (before or before_archive):
                    await go_back()
                    await _install_and_build(app, log)
                    await _restart(app_id, log, True)
                    status = "rolled-back"
                    log("Rolled back to the previous version.")
                else:
                    status = "failed"
            else:
                status = "ok"
        except DeployError as exc:
            log(f"FAILED: {exc}")
            status = "failed"
            message = message or str(exc)
        except Exception as exc:
            logging.exception("Deploy crashed")
            log(f"FAILED: {exc}")
            status = "failed"
        finally:
            log(f"Finished: {status}")
            log.close()
            if status == "ok":
                _add_release(app_id, "deploy", commit_sha, message, None, 0, by)
            _record(app_id, last_deploy_status=status, last_deploy_at=utc_now(),
                    last_deploy_commit=commit_sha if status == "ok" else None)
            _state[app_id].update(running=False, status=status, finished=utc_now().isoformat())
            row = _app(app_id)
            name = row.name if row else f"App {app_id}"
            audit_log(by, "app_deploy" if not target else "app_rollback", f"app '{name}' (id={app_id}): {status}")
            tail = "\n".join(log.lines[-8:])
            if status == "ok":
                notify.emit("deploy_ok", f"{name} deployed", f"{(commit_sha or '')[:10]} {message}".strip(), app_id, name)
            elif status == "rolled-back":
                notify.emit("deploy_rolled_back", f"{name}: deploy rolled back", tail, app_id, name, "error")
            else:
                notify.emit("deploy_failed", f"{name}: deploy failed", tail, app_id, name, "error")
        return {"status": status, "commit": commit_sha}


async def _chown_if_needed(app: App) -> None:
    if _app_ids(app):
        await asyncio.to_thread(_chown_app_dir, app)


# ---------------------------------------------------------------------------
# Push webhooks
# ---------------------------------------------------------------------------

def verify_webhook(headers, body: bytes, secret: str) -> bool:
    """GitHub / Gitea / Forgejo (HMAC of the body), GitLab (the token itself), or a
    plain `Authorization: Bearer <secret>` / `X-Venvy-Secret` from your own CI."""
    if not secret:
        return False
    sig = headers.get("x-hub-signature-256") or headers.get("x-gitea-signature") or headers.get("x-forgejo-signature") or ""
    if sig:
        sig = sig.split("=", 1)[1] if sig.startswith("sha256=") else sig
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig.lower(), want)
    for candidate in (headers.get("x-gitlab-token"), headers.get("x-venvy-secret"),
                      (headers.get("authorization") or "").removeprefix("Bearer ").strip() or None):
        if candidate and hmac.compare_digest(candidate.encode(), secret.encode()):
            return True
    return False


def webhook_branch(headers, body: bytes) -> Optional[str]:
    """The branch a push webhook is about, or None when the payload does not say."""
    try:
        ref = json.loads(body or b"{}").get("ref")
    except ValueError:
        return None
    if isinstance(ref, str) and ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return None
