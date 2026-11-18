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
from venvy.crypto import _decrypt_secret
from venvy.db import engine
from venvy.isolation import _app_ids, _app_tool_env, _as_app_user, _chown_app_dir, _chown_tree, _ensure_app_user
from venvy.models import App, Release

RELEASES_DIR = DATA_DIR / "releases"
DEPLOY_LOG_DIR = DATA_DIR / "deploy-logs"
DEPLOY_KEY_DIR = DATA_DIR / "deploy-keys"
KEEP_RELEASES = int(os.getenv("VENVY_KEEP_RELEASES", "10"))
MAX_KEEP_RELEASES = 50
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
    if len(u) > 2000:         # (and a regular expression never gets a megabyte to chew on)
        raise ValueError("The repository URL is too long.")
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
    """A deploy's log: kept in memory for the Deploy tab and written to the
    app's deploy log file. With persist=False (a snapshot taken outside a
    deploy) nothing is written, so the last deploy's log is not wiped out."""

    def __init__(self, app_id: int, secrets: List[str], persist: bool = True):
        self.app_id = app_id
        self.secrets = [s for s in secrets if s]
        self.lines: List[str] = []
        self.persist = persist
        self.fh = None
        if persist:
            DEPLOY_LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path = DEPLOY_LOG_DIR / f"{app_id}.log"
            self.fh = open(self.path, "w", encoding="utf-8")

    def __call__(self, line: str) -> None:
        # Longest first: a secret that contains another must not be left half masked.
        for s in sorted(self.secrets, key=len, reverse=True):
            line = line.replace(s, "***")
        self.lines.append(line)
        if self.persist and self.app_id in _state:
            _state[self.app_id]["log"] = self.lines
        if self.fh is not None:
            with contextlib.suppress(OSError):
                self.fh.write(line + "\n")
                self.fh.flush()

    def close(self) -> None:
        if self.fh is not None:
            with contextlib.suppress(OSError):
                self.fh.close()


# Values shorter than this are not masked in a deploy log: "1", "true" or a
# port number would turn every line into asterisks and hide nothing.
_MASK_MIN_LEN = 6


def _env_secrets(app: App) -> List[str]:
    """The values of the app's own and the shared environment variables: a build
    runs with them, and whatever it prints ends up in the deploy log."""
    from venvy import shared_env
    values = list(shared_env.load().values())
    with contextlib.suppress(ValueError, TypeError):
        own = json.loads(_decrypt_secret(app.env_json) or "{}")
        if isinstance(own, dict):
            values += [str(v) for v in own.values()]
    return sorted({v for v in values if len(v) >= _MASK_MIN_LEN}, key=len, reverse=True)


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
    userinfo = re.match(r"https://([^/@]+)@", url)
    if userinfo:
        log.secrets.append(userinfo.group(1))
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
        await proc.wait()
        raise DeployError(f"{what} timed out after {timeout} seconds.")
    if proc.returncode != 0:
        raise DeployError(f"{what} failed (exit code {proc.returncode}).")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Deploy keys
# ---------------------------------------------------------------------------

async def generate_deploy_key(app: App) -> str:
    """Make (or replace) the app's SSH deploy key; returns the public half.

    The key folder belongs to the app's user, who could swap the files in it
    for symlinks at any moment — and ssh-keygen and chown, run as root, would
    follow them (chown /etc/shadow over to the app, say). So the key is made
    in a new folder only root can enter, handed over there, and then put in
    place of the old folder with one rename."""
    import shutil
    import tempfile
    d = DEPLOY_KEY_DIR / str(app.id)
    DEPLOY_KEY_DIR.mkdir(mode=0o711, parents=True, exist_ok=True)
    ids = await asyncio.to_thread(_ensure_app_user, app)
    work = Path(tempfile.mkdtemp(prefix=f".new-{app.id}-", dir=DEPLOY_KEY_DIR))
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"venvy-app-{app.id}", "-f", str(work / "id_ed25519"),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise DeployError(f"ssh-keygen failed: {out.decode(errors='replace')[:300]}")
        public = (work / "id_ed25519.pub").read_text().strip()
        # The hosts already trusted stay trusted (read without following links).
        from venvy.safefs import AppPathError, _fs_open_file
        with contextlib.suppress(AppPathError, OSError):
            f, st = _fs_open_file(d, ("known_hosts",))
            with f:
                if st.st_size <= 1024 * 1024:
                    (work / "known_hosts").write_bytes(f.read())
        if ids:
            for p in (work / "id_ed25519", work / "id_ed25519.pub", work / "known_hosts", work):
                with contextlib.suppress(FileNotFoundError):
                    os.lchown(p, ids[0], ids[1])
        # DEPLOY_KEY_DIR is root's: once the old folder is gone, nothing but
        # this rename can put one there.
        await asyncio.to_thread(shutil.rmtree, d, True)
        os.rename(work, d)
    finally:
        await asyncio.to_thread(shutil.rmtree, work, True)
    return public


def _own_keys(app: App) -> None:
    """Hand the app's key folder to the app's user, who runs git and ssh. A key made
    before per-app users were switched on still belongs to the old service user, and
    ssh, running as the app, could not read it. Blocking."""
    d = DEPLOY_KEY_DIR / str(app.id)
    ids = _app_ids(app)
    if not ids or not d.is_dir() or d.is_symlink():
        return
    if d.stat().st_uid != ids[0]:
        _chown_tree(d, *ids)


def public_key(app_id: int) -> Optional[str]:
    # The folder is the app's: read without following a link it may have put
    # there (a public key that is really /etc/shadow).
    from venvy.safefs import AppPathError, _fs_open_file
    try:
        f, st = _fs_open_file(DEPLOY_KEY_DIR / str(app_id), ("id_ed25519.pub",))
    except (AppPathError, OSError):
        return None
    with f:
        if st.st_size > 16 * 1024:
            return None
        return f.read().decode("utf-8", errors="replace").strip() or None


def forget_keys(app_id: int) -> None:
    """Everything deploys kept for an app that is gone. App ids are handed out
    again, so the in-memory state goes too: the next app with this id must not
    be shown the last one's deploy log."""
    import shutil
    shutil.rmtree(DEPLOY_KEY_DIR / str(app_id), ignore_errors=True)
    shutil.rmtree(RELEASES_DIR / str(app_id), ignore_errors=True)
    (DEPLOY_LOG_DIR / f"{app_id}.log").unlink(missing_ok=True)
    _state.pop(app_id, None)
    lock = _locks.get(app_id)
    if lock is not None and not lock.locked():
        _locks.pop(app_id, None)


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
    # stderr is read alongside stdout: tar warns once per file it cannot read,
    # and with nobody reading, a full stderr pipe stopped tar — and the deploy
    # with it — for good.
    err_buf = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            data = await proc.stderr.read(65536)
            if not data:
                return
            if len(err_buf) < 64 * 1024:
                err_buf.extend(data)

    err_task = asyncio.ensure_future(drain_stderr())
    try:
        with open(dest, "wb") as f:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > SNAPSHOT_MAX_BYTES:
                    proc.kill()
                    await proc.wait()
                    dest.unlink(missing_ok=True)
                    raise DeployError("The app's files are too large to snapshot (over 2 GB).")
                f.write(chunk)
        await proc.wait()
        await err_task
    finally:
        if not err_task.done():
            err_task.cancel()
    err = bytes(err_buf)
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


def keep_limit(app_id: int) -> int:
    """How many releases an app keeps: its own setting, else the server default."""
    with Session(engine) as s:
        app = s.get(App, app_id)
    n = app.keep_releases if app and app.keep_releases else KEEP_RELEASES
    return max(1, min(n, MAX_KEEP_RELEASES))


def _delete_release_rows(s: Session, app_id: int, rows) -> int:
    for r in rows:
        if r.archive:
            (RELEASES_DIR / str(app_id) / r.archive).unlink(missing_ok=True)
        s.delete(r)
    return len(rows)


def _prune_releases(app_id: int, protect: Optional[int] = None) -> int:
    """Delete all but the newest keep_limit() releases. *protect* (a release a
    running rollback is about to restore) is never deleted, however old."""
    keep = keep_limit(app_id)
    with Session(engine) as s:
        rows = s.exec(select(Release).where(Release.app_id == app_id).order_by(Release.id.desc())).all()
        n = _delete_release_rows(s, app_id, [r for r in rows[keep:] if r.id != protect])
        s.commit()
    return n


def delete_release(app_id: int, release_id: int) -> bool:
    """Remove one release (and its snapshot file). False if there is no such release."""
    with Session(engine) as s:
        rel = s.get(Release, release_id)
        if not rel or rel.app_id != app_id:
            return False
        _delete_release_rows(s, app_id, [rel])
        s.commit()
    return True


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
                 size: int, by: str, protect: Optional[int] = None) -> None:
    if not (commit or archive):
        # Nothing to go back to (a build-only deploy, a snapshot rollback): such a
        # row only pushed restorable releases, and their archives, out of
        # KEEP_RELEASES — with every deploy of a build-only app, half of them.
        return
    with Session(engine) as s:
        if kind == "before-deploy" and commit:
            # Usually what the last deploy left: already in the list, and a
            # copy would only push older releases out of KEEP_RELEASES.
            newest = s.exec(select(Release).where(Release.app_id == app_id).order_by(Release.id.desc())).first()
            if newest and newest.commit == commit:
                return
        s.add(Release(app_id=app_id, kind=kind, commit=commit, message=message[:300], archive=archive, size=size, by=by))
        s.commit()
    _prune_releases(app_id, protect)


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
        # Checked here and not only by the routes: a push webhook arrives whenever it likes.
        if app.status == "installing":
            raise DeployError("This app is still being set up.")
        from venvy.process import restore_in_progress
        if restore_in_progress():
            raise DeployError("A restore is in progress.")
        _state[app_id] = {"running": True, "status": "running", "log": [], "started": utc_now().isoformat(),
                          "finished": None, "trigger": trigger}
        log = _Log(app_id, _env_secrets(app))
        _state[app_id]["log"] = log.lines
        _record(app_id, last_deploy_status="running", last_deploy_at=utc_now())
        what = "Rolling back" if target else "Deploying"
        log(f"{what} {app.name} ({trigger}, by {by})")
        status, commit_sha, message = "failed", None, ""
        try:
            await asyncio.to_thread(_ensure_app_user, app)
            await asyncio.to_thread(_own_keys, app)
            was_running = app.status in ("running", "starting")
            before = await current_commit(app)
            before_archive: Optional[Tuple[str, int]] = None
            # Not a git checkout yet: a snapshot is the only way back — also on the
            # first deploy from a repository, which checks the repository's files out
            # over whatever was uploaded before. (Only apps that were not a
            # checkout and had no repository used to get one; a failed first git
            # deploy had nothing to return to.)
            made_repo = before is None and not _is_repo(app)
            if before is None:
                try:
                    before_archive = await _snapshot(app, log)
                except DeployError as exc:
                    if not app.git_url:
                        raise
                    # A first deploy from git used to go ahead without one; it still may.
                    log(f"No snapshot ({exc}) — if this deploy fails, the old files cannot be put back.")
            # What to go back to if this goes wrong.
            if before or before_archive:
                _add_release(app_id, "before-deploy", before[0] if before else None, before[1] if before else "",
                             before_archive[0] if before_archive else None, before_archive[1] if before_archive else 0, by,
                             # With few releases kept, the one being rolled back to may be the
                             # oldest: pruned now, its snapshot would be gone before the restore.
                             protect=target.id if target is not None else None)

            async def go_back() -> None:
                log("Putting the previous version back…")
                if before:
                    await _git(app, log, ["checkout", "-f", "-B", app.git_branch, before[0]])
                elif before_archive:
                    if made_repo and _is_repo(app):
                        # The checkout this deploy made goes with it: left behind, the
                        # next deploy would take the failed commit for the version to
                        # return to.
                        await _run(app, ["rm", "-rf", "--", ".git"], log, _app_tool_env(app), app.app_dir, 300,
                                   "remove the new git checkout")
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
            # A rollback target spared above, or a lower keep_releases saved while
            # this ran (put_git leaves pruning to the deploy then).
            with contextlib.suppress(Exception):
                _prune_releases(app_id)
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
        # Compared as bytes: compare_digest() raises on a str that isn't ASCII,
        # which turned a garbled header into a 500 instead of a 401.
        return hmac.compare_digest(sig.lower().encode("utf-8"), want.encode("ascii"))
    for candidate in (headers.get("x-gitlab-token"), headers.get("x-venvy-secret"),
                      (headers.get("authorization") or "").removeprefix("Bearer ").strip() or None):
        if candidate and hmac.compare_digest(candidate.encode(), secret.encode()):
            return True
    return False


def webhook_event(headers) -> Optional[str]:
    """The event a git host says it is sending ("push", "ping", ...), or None when
    the request does not say (your own CI): GitLab's "Push Hook" counts as "push"."""
    event = (headers.get("x-github-event") or headers.get("x-gitea-event")
             or headers.get("x-forgejo-event") or headers.get("x-gitlab-event"))
    if event is None:
        return None
    event = event.strip().lower()
    return {"push hook": "push", "system hook": "system"}.get(event, event)


_ZERO_SHA = re.compile(r"0{40}(0{24})?")


def webhook_push(headers, body: bytes) -> Tuple[Optional[str], Optional[str]]:
    """(branch, reason not to deploy) for a push webhook. The branch is None when
    the payload does not name one (your own CI): that deploys. A push of a tag, or
    one that deleted a branch, names no branch to deploy and gives a reason."""
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return None, None
    # A body that is JSON but not an object (a list, a string) says nothing
    # about a branch; .get() on it used to answer the hook with a 500.
    if not isinstance(payload, dict):
        return None, None
    ref = payload.get("ref")
    if not isinstance(ref, str):
        return None, None
    if not ref.startswith("refs/heads/"):
        # A tag push (refs/tags/…) used to count as "no branch named" and deployed.
        return None, f"push to {ref[:200]}, not a branch"
    branch = ref[len("refs/heads/"):]
    after = payload.get("after")
    if payload.get("deleted") is True or (isinstance(after, str) and _ZERO_SHA.fullmatch(after)):
        return branch, f"branch {branch[:200]} was deleted"
    return branch, None


def webhook_branch(headers, body: bytes) -> Optional[str]:
    """The branch a push webhook is about, or None when the payload does not say."""
    return webhook_push(headers, body)[0]
