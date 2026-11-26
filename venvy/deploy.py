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
from venvy.crypto import _decrypt_secret, _try_decrypt_secret
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
                     "./.cache", "*/__pycache__", "*.pyc", "./.venvy", "./.cargo", "./go/pkg", "./.npm",
                     "./.bun", "./.deno", "./.rustup", "./.gradle", "./.m2", "./.local/share/pnpm")

_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/\-]{0,100}")
_SHA_RE = re.compile(r"[0-9a-f]{7,40}")
_URL_RE = re.compile(
    r"(https://[A-Za-z0-9][A-Za-z0-9.\-]*(?::\d+)?/\S+"
    r"|ssh://(?:[A-Za-z0-9._\-]+@)?[A-Za-z0-9][A-Za-z0-9.\-]*(?::\d+)?/\S+"
    r"|[A-Za-z0-9._\-]+@[A-Za-z0-9][A-Za-z0-9.\-]*:\S+)"
)
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


def clean_branch_ok(branch: str) -> bool:
    return bool(_BRANCH_RE.fullmatch(branch or "")) and ".." not in branch


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

    _MAX_LOG_LINES = 2000

    def __init__(self, app_id: int, secrets: List[str], persist: bool = True):
        self.app_id = app_id
        # Longest first: a secret that contains another must not be left half masked.
        self.secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self.lines: List[str] = []
        self.persist = persist
        self.fh = None
        if persist:
            DEPLOY_LOG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path = DEPLOY_LOG_DIR / f"{app_id}.log"
            self.fh = open(self.path, "w", encoding="utf-8")

    def add_secrets(self, *values: str) -> None:
        """Add secrets and keep longest-first order without duplicates."""
        merged = set(self.secrets) | {v for v in values if v}
        self.secrets = sorted(merged, key=len, reverse=True)

    def __call__(self, line: str) -> None:
        for s in self.secrets:
            line = line.replace(s, "***")
        self.lines.append(line)
        if len(self.lines) > self._MAX_LOG_LINES:
            del self.lines[:len(self.lines) - self._MAX_LOG_LINES]
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


def _env_secrets(app: App, strict: bool = False) -> List[str]:
    """The values of the app's own and the shared environment variables: a build
    runs with them, and whatever it prints ends up in the deploy log."""
    from venvy import shared_env
    from venvy.crypto import _try_decrypt_secret
    values = list(shared_env.load(strict=strict).values())
    ok, plain = _try_decrypt_secret(app.env_json)
    if strict and not ok:
        raise RuntimeError("Could not decrypt app environment variables.")
    if ok and plain:
        try:
            own = json.loads(plain)
            if isinstance(own, dict):
                values += [str(v) for v in own.values()]
        except (ValueError, TypeError):
            if strict:
                raise
    return sorted({v for v in values if len(v) >= _MASK_MIN_LEN}, key=len, reverse=True)


# ---------------------------------------------------------------------------
# Running commands as the app
# ---------------------------------------------------------------------------

def _app_env_for_commands(app: App) -> Dict[str, str]:
    from venvy import appspec
    from venvy.process import _build_app_env
    env = _build_app_env(app)
    if _app_ids(app):
        from venvy.isolation import _app_username
        owner = appspec.owner_id(app)
        env.update(HOME=str(appspec.home_dir(app)), USER=_app_username(owner), LOGNAME=_app_username(owner))
    return env


def _git_env(app: App, log: _Log) -> Dict[str, str]:
    env = _app_tool_env(app)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh" + (":file" if ALLOW_LOCAL_GIT else "")   # never ext::
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    url = app.git_url or ""
    userinfo = re.match(r"(?:https|ssh)://([^/@]+)@", url)
    if userinfo:
        log.add_secrets(userinfo.group(1))
    auth = {}
    if app.git_auth:
        with contextlib.suppress(ValueError):
            auth = json.loads(_decrypt_secret(app.git_auth) or "{}")
    if url.startswith("https://") and auth.get("token"):
        import urllib.parse as _up
        parsed = _up.urlsplit(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        basic = base64.b64encode(f"x-access-token:{auth['token']}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"http.https://{host}/.extraHeader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {basic}"
        log.add_secrets(auth["token"], basic)
    elif is_ssh_url(url):
        key = DEPLOY_KEY_DIR / str(app.id) / "id_ed25519"
        known = DEPLOY_KEY_DIR / str(app.id) / "known_hosts"
        parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", f"UserKnownHostsFile={known}"]
        if key.exists():
            parts += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
        env["GIT_SSH_COMMAND"] = " ".join(shlex.quote(p) for p in parts)
    return env


_DEPLOY_MAX_LINE_BYTES = 64 * 1024


async def _run(app: App, argv: List[str], log: _Log, env: Dict[str, str], cwd: str,
               timeout: int, what: str, as_venvy: bool = False) -> str:
    """Run argv as the app's user, logging its output; returns it. Raises DeployError.
    *as_venvy*: as Venvy itself, with a clean environment (docker build)."""
    from venvy.isolation import _clean_root_env, _exec_env_argv, _isolation_active, _prepare_exec_env
    log(f"$ {what}")
    env_tf = None
    try:
        if as_venvy:
            spawn_argv = list(argv)
            spawn_env = {**_clean_root_env(), "DOCKER_BUILDKIT": "1", "PATH": runtimes.tool_search_path()}
            pass_fds = ()
        elif _isolation_active():
            env_tf, env_fd = _prepare_exec_env(env)
            spawn_argv = _as_app_user(app, _exec_env_argv(env_fd, argv))
            spawn_env = _clean_root_env()
            pass_fds = (env_fd,)
        else:
            spawn_argv = list(argv)
            spawn_env = env
            pass_fds = ()
        proc = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=spawn_env, cwd=cwd, start_new_session=True, pass_fds=pass_fds)
    except FileNotFoundError:
        raise DeployError(f"{argv[0]} is not installed on this host.")
    finally:
        if env_tf is not None:
            env_tf.close()
    out: List[str] = []

    async def pump() -> None:
        assert proc.stdout is not None
        pending = b""
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                if pending:
                    line = pending.decode(errors="replace").rstrip("\r\n")
                    out.append(line)
                    log("  " + line)
                return
            pending += chunk
            while True:
                idx = pending.find(b"\n")
                if idx != -1:
                    raw_line = pending[:idx]
                    pending = pending[idx + 1:]
                elif len(pending) >= _DEPLOY_MAX_LINE_BYTES:
                    raw_line = pending[:_DEPLOY_MAX_LINE_BYTES]
                    pending = pending[_DEPLOY_MAX_LINE_BYTES:]
                else:
                    break
                line = raw_line.decode(errors="replace").rstrip("\r\n")
                out.append(line)
                log("  " + line)

    try:
        await asyncio.wait_for(asyncio.gather(pump(), proc.wait()), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise DeployError(f"{what} timed out after {timeout} seconds.")
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
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
                    data = f.read(1024 * 1024 + 1)
                    if len(data) <= 1024 * 1024:
                        (work / "known_hosts").write_bytes(data)
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
        data = f.read(16 * 1024 + 1)
        if len(data) > 16 * 1024:
            return None
        return data.decode("utf-8", errors="replace").strip() or None


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
    env = _git_env(app, log)
    # A proxy the app writes into .git/config would see the deploy token, so
    # http.proxy is always set here — to the one this host is configured with
    # (an empty value would turn the https_proxy/all_proxy variables off too).
    # Passed in the environment, not as -c: a proxy URL may carry a password,
    # and every user on the host can read a process's command line.
    proxy = next((env[k] for k in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY")
                  if env.get(k)), "")
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = "http.proxy", proxy
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    safe_git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    return await _run(app, safe_git, log, env, app.app_dir, timeout, "git " + " ".join(args))


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
    await _git(app, log, ["remote", "add", "origin", "--", app.git_url or ""])
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
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.kill()
                    await proc.wait()
                    dest.unlink(missing_ok=True)
                    raise DeployError("The app's files are too large to snapshot (over 2 GB).")
                await asyncio.to_thread(f.write, chunk)
        await proc.wait()
        await err_task
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        dest.unlink(missing_ok=True)
        raise
    finally:
        if not err_task.done():
            err_task.cancel()
    err = bytes(err_buf)
    if proc.returncode not in (0, 1):            # 1: a file changed while being read
        dest.unlink(missing_ok=True)
        raise DeployError(f"tar failed: {err.decode(errors='replace')[:300]}")
    os.chmod(dest, 0o600)
    return name, size


_RESTORE_KEEP_TOP = {"venv", "node_modules", "target", "logs", ".git", ".cache", "vendor"}


async def _restore_snapshot(app: App, archive: str, log: _Log, src_dir: Optional[Path] = None) -> None:
    path = (src_dir or RELEASES_DIR / str(app.id)) / archive
    if not path.is_file() or Path(archive).name != archive:
        raise DeployError("That snapshot is gone.")
    log("Restoring the snapshot…")
    proc = await asyncio.create_subprocess_exec(
        *_as_app_user(app, ["tar", "-xzf", "-", "-C", app.app_dir, "--no-same-owner"]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        env=_app_tool_env(app), start_new_session=True)
    assert proc.stdin is not None and proc.stderr is not None
    err_buf = bytearray()

    async def drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            data = await proc.stderr.read(65536)
            if not data:
                return
            if len(err_buf) < 64 * 1024:
                err_buf.extend(data)

    async def feed_stdin() -> None:
        assert proc.stdin is not None
        try:
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
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    try:
        await asyncio.wait_for(asyncio.gather(feed_stdin(), drain_stderr(), proc.wait()), timeout=600)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        raise DeployError("Restoring snapshot timed out.")
    except BaseException:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    if proc.returncode != 0:
        raise DeployError(f"tar failed: {bytes(err_buf).decode(errors='replace')[:300]}")


def keep_limit(app_id: int) -> int:
    """How many releases an app keeps: its own setting, else the server default."""
    with Session(engine) as s:
        app = s.get(App, app_id)
    n = app.keep_releases if app and app.keep_releases else KEEP_RELEASES
    return max(1, min(n, MAX_KEEP_RELEASES))


def _delete_release_rows(s: Session, app_id: int, rows) -> List[Path]:
    to_unlink: List[Path] = []
    for r in rows:
        if r.archive and Path(r.archive).name == r.archive:
            to_unlink.append(RELEASES_DIR / str(app_id) / r.archive)
        s.delete(r)
    return to_unlink


def _prune_releases(app_id: int, protect: Optional[int] = None) -> int:
    """Delete all but the newest keep_limit() releases. *protect* (a release a
    running rollback is about to restore) is never deleted, however old."""
    keep = keep_limit(app_id)
    with Session(engine) as s:
        rows = s.exec(select(Release).where(Release.app_id == app_id).order_by(Release.id.desc())).all()
        pruned = [r for r in rows[keep:] if r.id != protect]
        to_unlink = _delete_release_rows(s, app_id, pruned)
        s.commit()
    for p in to_unlink:
        p.unlink(missing_ok=True)
    return len(pruned)


def delete_release(app_id: int, release_id: int) -> bool:
    """Remove one release (and its snapshot file). False if there is no such release."""
    with Session(engine) as s:
        rel = s.get(Release, release_id)
        if not rel or rel.app_id != app_id:
            return False
        to_unlink = _delete_release_rows(s, app_id, [rel])
        s.commit()
    for p in to_unlink:
        p.unlink(missing_ok=True)
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


async def _install_and_build(app: App, log: _Log, build: bool = True, release: bool = True) -> None:
    """Dependencies (the manifest, or the install command), the runtime's own
    build (cargo build --release, go build, mvn package, npm run build, docker
    build), the build command, then — when *release* — the release command
    (migrations), which runs only once the build succeeded."""
    from venvy import appspec, toolchains
    wd = appspec.work_dir(app)
    problem = appspec.work_dir_problem(app)
    if problem:
        raise DeployError(problem)
    try:
        await toolchains.ensure_for_app(app, wd, log)
    except Exception as exc:
        raise DeployError(f"Could not install the toolchain: {exc}")
    tool_env = _app_tool_env(app)
    cmd_env = {**_app_env_for_commands(app), **appspec.cache_env(app)}
    cmd_env["PATH"] = tool_env["PATH"]
    if app.runtime == "python" and not (Path(app.venv_path) / "bin" / "python").exists():
        from venvy import runtimes as _rt
        py = _rt.python_binary(app.runtime_version)
        await _run(app, [py, "-m", "venv", app.venv_path], log, tool_env, "/", 300, "python -m venv")
    if app.install_command:
        await _run(app, appspec.shell_argv(app.install_command), log, cmd_env, str(wd), 1800, "install: (command)")
    else:
        for step in await asyncio.to_thread(runtimes.sync_steps, app.runtime, wd, False, app.venv_path):
            await _run(app, step.argv, log, {**tool_env, **dict(step.env)}, str(wd) if step.in_app_dir else "/",
                       step.timeout, step.what, as_venvy=step.as_venvy)
    if build:
        for step in await asyncio.to_thread(runtimes.build_steps, app.runtime, wd, app.entrypoint, app):
            await _run(app, step.argv, log, {**cmd_env, **dict(step.env)}, str(wd) if step.in_app_dir else "/",
                       step.timeout, step.what, as_venvy=step.as_venvy)
        if app.build_command:
            # Don't echo the raw build command into logs; it may carry inline secrets.
            await _run(app, ["sh", "-c", app.build_command], log, cmd_env, str(wd), 3600, "build: (command)")
    if release and app.release_command:
        await _run(app, appspec.shell_argv(app.release_command), log, cmd_env, str(wd), 1800, "release: (command)")


async def _restart(app_id: int, log: _Log, start_if_stopped: bool) -> None:
    from venvy import groups
    from venvy.process import start_app_process_logic
    app = _app(app_id)
    assert app
    if app.status in ("running", "starting"):
        log("Restarting the app…" + (" (one process at a time)" if app.instances > 1 or app.zero_downtime else ""))
        await groups.restart_group(app_id)
    elif start_if_stopped:
        log("Starting the app…")
        with Session(engine) as s:
            await start_app_process_logic(app_id, s)
        await groups.start_children(app_id)


async def _wait_healthy(app_id: int, log: _Log) -> bool:
    """True once the restarted app looks alive: its health check passes, or — with
    no health check — it is still running a few seconds after the start."""
    app = _app(app_id)
    if not app:
        return False
    deadline = time.monotonic() + max(HEALTH_WAIT_SEC, int(app.health_start_period or 0) + 30)
    await asyncio.sleep(3)
    app = _app(app_id)
    if not app:
        return False
    checking = monitor.has_check(app)
    if checking:
        log(f"Waiting for {monitor.describe_check(app)} to answer…")
    else:
        await asyncio.sleep(3)
    while time.monotonic() < deadline:
        app = _app(app_id)
        if not app or app.status == "error":
            return False
        if app.status == "running":
            if not monitor.has_check(app):
                return True
            if await monitor.check_once(app):
                return True
        elif app.status == "stopped":
            return False
        await asyncio.sleep(2)
    return False


# ---------------------------------------------------------------------------
# The repository describing itself (venvy.toml, Procfile)
# ---------------------------------------------------------------------------

async def _apply_repo_spec(app_id: int, wd: Path, log: _Log) -> None:
    """Take venvy.toml and the Procfile of the code just checked out: their
    settings go into the app, [processes] / Procfile lines become workers,
    [env] fills in variables the app doesn't set itself."""
    from venvy import appspec, groups
    from venvy.api.apps import apply_spec_fields
    try:
        spec, procs = await asyncio.to_thread(appspec.read_repo_spec, wd)
    except appspec.SpecError as exc:
        raise DeployError(str(exc))
    if spec is None and not procs:
        return
    res: Dict[str, List] = {"added": [], "changed": [], "removed": []}
    removed: List[int] = []
    with Session(engine) as s:
        row = s.get(App, app_id)
        if row is None:
            return
        if spec:
            try:
                changed = apply_spec_fields(row, spec["fields"])
            except ValueError as exc:
                raise DeployError(f"venvy.toml: {exc}")
            if spec["env"]:
                from venvy.crypto import _encrypt_secret
                ok, plain = _try_decrypt_secret(row.env_json)
                cur = json.loads(plain or "{}") if ok else {}
                added = [k for k in spec["env"] if k not in cur]
                for k in added:
                    cur[k] = spec["env"][k]
                if added:
                    row.env_json = _encrypt_secret(json.dumps(cur))
                    changed.append("env " + ",".join(added))
            if changed:
                log("venvy.toml: " + ", ".join(changed))
            procs = {**procs, **spec["processes"]}
        if "web" in procs and not (spec and "start_command" in spec["fields"]):
            row.start_command = procs["web"]
            log("Procfile: web process sets the start command.")
        s.add(row)
        s.commit()
        s.refresh(row)
        if row.parent_app_id is None:
            res = groups.sync_workers(s, row, procs)
            removed = groups.sync_replicas(s, row)
            if res["added"] or res["changed"] or res["removed"]:
                log(f"Processes: added {', '.join(res['added']) or 'none'}, "
                    f"changed {len(res['changed'])}, removed {len(res['removed'])}.")
    if res["removed"] or removed:
        await groups.delete_children(res["removed"] + removed)


# ---------------------------------------------------------------------------
# Building somewhere else, then switching (atomic deploys, artifacts)
# ---------------------------------------------------------------------------

_CARRY_DIRS = ("venv", "node_modules", "target", ".venvy", "vendor", ".git")
_CARRY_GLOBS = (".env", ".env.*", "*.db", "*.sqlite", "*.sqlite3")
_RELOCATE = Path(__file__).parent / "runtime_assets" / "relocate.py"


_GNU_CP: Optional[bool] = None


def _cp_argv(src: str, dst: str) -> List[str]:
    """cp -a, with copy-on-write clones where the filesystem and GNU cp allow."""
    global _GNU_CP
    if _GNU_CP is None:
        import subprocess
        try:
            _GNU_CP = b"GNU" in subprocess.run(["cp", "--version"], capture_output=True, timeout=10).stdout
        except Exception:
            _GNU_CP = False
    return ["cp", "-a", "--reflink=auto", src, dst] if _GNU_CP else ["cp", "-a", src, dst]


def _staging_root(app: App) -> Path:
    from venvy import appspec
    return appspec.state_root(app) / "staging"


async def _make_staging(app: App, log: _Log) -> Path:
    """An empty folder, owned by the app's user, next to its state."""
    from venvy import appspec
    ids = await asyncio.to_thread(_ensure_app_user, app)
    root = _staging_root(app)
    def prep() -> Path:
        appspec.ensure_state_dirs(app, ids)
        if root.is_symlink():
            root.unlink()
        root.mkdir(mode=0o711, exist_ok=True)
        new = root / "new"
        if new.exists() or new.is_symlink():
            import shutil
            shutil.rmtree(new, ignore_errors=True)
            if new.is_symlink():
                new.unlink()
        new.mkdir(mode=0o700)
        if ids:
            os.chown(new, ids[0], ids[1], follow_symlinks=False)
        return new
    return await asyncio.to_thread(prep)


def _as(app: App, app_dir: Path) -> App:
    """A copy of *app* that lives in *app_dir* (for building in staging)."""
    copy = App(**{k: v for k, v in app.model_dump().items()})
    copy.app_dir = str(app_dir)
    try:
        copy.venv_path = str(app_dir / Path(app.venv_path).relative_to(app.app_dir))
    except ValueError:
        copy.venv_path = str(app_dir / "venv")
    return copy


def _renameat2_exchange(a: str, b: str) -> bool:
    """Swap two paths in one step (Linux renameat2 RENAME_EXCHANGE)."""
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        fn = libc.renameat2
    except (OSError, AttributeError):
        return False
    AT_FDCWD, RENAME_EXCHANGE = -100, 2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    return fn(AT_FDCWD, a.encode(), AT_FDCWD, b.encode(), RENAME_EXCHANGE) == 0


def _swap(live: Path, new: Path) -> None:
    """Put *new* where *live* is; the old tree ends up where *new* was."""
    if _renameat2_exchange(str(live), str(new)):
        return
    tmp = new.with_name("old-" + os.urandom(3).hex())
    os.rename(live, tmp)
    try:
        os.rename(new, live)
    except OSError:
        os.rename(tmp, live)
        raise
    os.rename(tmp, new)


async def _stage_copy(app: App, staging: Path, log: _Log) -> None:
    log("Copying the app into a build folder (atomic deploy)…")
    await _run(app, _cp_argv(f"{app.app_dir}/.", str(staging)), log, _app_tool_env(app), "/",
               1800, "copy to build folder")


async def _stage_archive(app: App, staging: Path, archive: Path, log: _Log, carry: bool = True) -> None:
    """Unpack *archive* into staging; bring along what's built (venv,
    node_modules, target …) and the files that are data (.env, *.db) from
    the live app, unless the archive has its own."""
    log("Unpacking the artifact…")
    proc = await asyncio.create_subprocess_exec(
        *_as_app_user(app, ["tar", "-xzf", "-", "-C", str(staging), "--no-same-owner", "--no-same-permissions"]),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        env=_app_tool_env(app), start_new_session=True)
    with open(archive, "rb") as f:
        data_iter = iter(lambda: f.read(1 << 20), b"")
        assert proc.stdin is not None
        try:
            for chunk in data_iter:
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()
    _, err = await asyncio.wait_for(proc.communicate(), timeout=1200)
    if proc.returncode != 0:
        raise DeployError("The artifact could not be unpacked: " + err.decode(errors="replace")[:300])
    if not carry:
        return
    live = Path(app.app_dir)
    from venvy import appspec
    keep = list(_CARRY_GLOBS)
    try:
        spec, _ = await asyncio.to_thread(appspec.read_repo_spec, staging / (app.root_dir or ""))
        extra = (spec or {}).get("keep") or []
    except appspec.SpecError:
        extra = []
    names = set()
    for d in _CARRY_DIRS:
        names.add(d)
    for pattern in keep + [str(x) for x in extra if isinstance(x, str)]:
        for m in live.glob(pattern):
            if m.parent == live:
                names.add(m.name)
    for name in sorted(names):
        src, dst = live / name, staging / name
        if (src.exists() and not src.is_symlink()) and not dst.exists():
            await _run(app, _cp_argv(str(src), str(dst)), log, _app_tool_env(app), "/", 1800, f"keep {name}")


async def _relocate_venv(app: App, staged: App, log: _Log) -> None:
    if app.runtime != "python" or not (Path(staged.venv_path) / "bin").is_dir():
        return
    from venvy.isolation import _resolve_system_bin
    await _run(staged, [_resolve_system_bin("python3") if _app_ids(app) else "python3", "-I", str(_RELOCATE),
                        staged.venv_path, staged.app_dir, app.app_dir], log, _app_tool_env(staged), "/", 300,
               "relocate the virtualenv")


async def _stage_and_switch(app: App, log: _Log, fill, before_switch=None) -> Path:
    """Build a new tree in staging with *fill(staged_app, staging_dir)*, then
    switch it in. Returns where the old tree is now (to switch back to)."""
    staging = await _make_staging(app, log)
    staged = _as(app, staging)
    await fill(staged, staging)
    await _chown_if_needed(staged)
    if app.spec_from_repo:
        await _apply_repo_spec(app.id, staging / (app.root_dir or ""), log)
        fresh = _app(app.id)
        if fresh is not None:
            app = fresh
            staged = _as(app, staging)
    await _install_and_build(staged, log)
    await _relocate_venv(app, staged, log)
    if before_switch is not None:
        await before_switch()
    log("Switching to the new build…")
    await asyncio.to_thread(_swap, Path(app.app_dir), staging)
    return staging


async def _switch_back(app: App, old: Path, log: _Log) -> None:
    log("Switching back to the previous build…")
    await asyncio.to_thread(_swap, Path(app.app_dir), old)


# ---------------------------------------------------------------------------
# Reporting to the git host
# ---------------------------------------------------------------------------

def commit_status_request(git_url: str, token: str, sha: str, state: str, description: str, context: str):
    """(url, headers, json body) of the commit-status call for the host of *git_url*
    — GitHub, GitLab (any host with gitlab in its name) or Gitea / Forgejo."""
    import urllib.parse as up
    parts = up.urlsplit(git_url)
    host = parts.hostname or ""
    path = parts.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    body = {"state": state, "description": description[:140], "context": context}
    if host == "github.com":
        return (f"https://api.github.com/repos/{path}/statuses/{sha}",
                {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, body)
    if "gitlab" in host:
        return (f"https://{parts.netloc}/api/v4/projects/{up.quote(path, safe='')}/statuses/{sha}",
                {"PRIVATE-TOKEN": token},
                {"state": {"failure": "failed"}.get(state, state), "description": description[:140], "name": context})
    return (f"https://{parts.netloc}/api/v1/repos/{path}/statuses/{sha}", {"Authorization": f"token {token}"}, body)


async def report_commit_status(app: App, sha: Optional[str], state: str, description: str) -> None:
    """Tell GitHub / GitLab / Gitea / Forgejo how the deploy of *sha* went
    (state: pending | success | failure), with the app's git token."""
    if not app.commit_status or not sha or not app.git_url or not app.git_url.startswith("https://"):
        return
    auth = {}
    if app.git_auth:
        with contextlib.suppress(ValueError):
            auth = json.loads(_decrypt_secret(app.git_auth) or "{}")
    token = auth.get("token")
    if not token:
        return
    import aiohttp
    from venvy.config import HTTP_CLIENT_LIMITS
    url, headers, body = commit_status_request(app.git_url, token, sha, state, description, f"venvy/{app.name}")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), **HTTP_CLIENT_LIMITS) as http:
            async with http.post(url, json=body, headers=headers) as r:
                if r.status >= 300:
                    logging.info("Commit status for %s: HTTP %s", app.name, r.status)
    except Exception as exc:
        logging.info("Commit status for %s failed: %s", app.name, exc)


_pending: Dict[int, tuple] = {}


async def run_deploy(app_id: int, by: str, trigger: str = "manual", target: Optional[Release] = None,
                     start_if_stopped: bool = False, artifact: Optional[Path] = None) -> dict:
    """Deploy (or, with *target*, roll back to) an app — from its git repository,
    from an uploaded *artifact* (a .tar.gz of the app's files), or just
    rebuilding what it has. Raises DeployError if it cannot start, returns
    {"status": "ok" | "failed" | "rolled-back" | "queued", "commit": ...} otherwise.

    A deploy asked for while one is running is queued (the newest request
    wins) and runs when the current one is done."""
    lock = _lock(app_id)
    if lock.locked():
        if target is not None:
            raise DeployError("A deploy is already running for this app.")
        old = _pending.get(app_id)
        if old and old[3] and old[3] != artifact:
            with contextlib.suppress(OSError):
                old[3].unlink()
        _pending[app_id] = (by, trigger, start_if_stopped, artifact)
        return {"status": "queued", "commit": None}
    try:
        return await _run_deploy_locked(app_id, by, trigger, target, start_if_stopped, artifact)
    finally:
        nxt = _pending.pop(app_id, None)
        if nxt is not None:
            from venvy.jobs import _running_tasks
            t = asyncio.ensure_future(run_deploy(app_id, nxt[0], nxt[1] + " (queued)", None, nxt[2], nxt[3]))
            _running_tasks.add(t)
            t.add_done_callback(_running_tasks.discard)


async def _run_deploy_locked(app_id: int, by: str, trigger: str, target: Optional[Release],
                             start_if_stopped: bool, artifact: Optional[Path]) -> dict:
    lock = _lock(app_id)
    async with lock:
        app = _app(app_id)
        if not app:
            raise DeployError("App not found.")
        if app.parent_app_id:
            raise DeployError("Deploy the app this process belongs to.")
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
        atomic = app.deploy_strategy == "atomic" or artifact is not None
        old_tree: Optional[Path] = None
        artifact_release: Optional[Tuple[str, int]] = None
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
            if before is None and not atomic:
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
                if old_tree is not None:
                    await _switch_back(app, old_tree, log)
                    return
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

            target_archive = (RELEASES_DIR / str(app_id) / target.archive) if target is not None and target.archive else None
            if atomic:
                if artifact is not None:
                    async def fill(staged: App, staging: Path) -> None:
                        await _stage_archive(app, staging, artifact, log)
                elif target_archive is not None:
                    async def fill(staged: App, staging: Path) -> None:
                        await _stage_archive(app, staging, target_archive, log)
                else:
                    async def fill(staged: App, staging: Path) -> None:
                        await _stage_copy(app, staging, log)
                        if target is not None and target.commit:
                            if not (staging / ".git").exists():
                                raise DeployError("This release is a git commit, but the app has no git checkout.")
                            await _git(staged, log, ["checkout", "-f", "-B", app.git_branch, target.commit])
                        elif target is None and app.git_url:
                            await _checkout_remote(staged, log)
                old_tree = await _stage_and_switch(app, log, fill)
                app = _app(app_id) or app
            else:
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
                    if app.spec_from_repo and target is None:
                        from venvy import appspec
                        await _apply_repo_spec(app_id, appspec.work_dir(app), log)
                        app = _app(app_id) or app
                    await _install_and_build(app, log)
                except Exception:
                    if target is None and (before or before_archive):
                        with contextlib.suppress(Exception):
                            await go_back()
                            await _install_and_build(app, log, release=False)
                    raise

            now = await current_commit(app)
            if now:
                commit_sha, message = now
                log(f"Now at {commit_sha[:10]} — {message}")
                await report_commit_status(app, commit_sha, "pending", "Deploying…")
            elif artifact is not None:
                message = f"artifact {artifact.name}"
            await _restart(app_id, log, start_if_stopped)
            fresh = _app(app_id)
            should_run = was_running or start_if_stopped or (fresh and fresh.status in ("running", "starting"))
            if should_run and not await _wait_healthy(app_id, log):
                log("The app did not come up healthy.")
                if old_tree is not None or (target is None and (before or before_archive)):
                    await go_back()
                    if old_tree is None:
                        await _install_and_build(app, log, release=False)
                    await _restart(app_id, log, True)
                    status = "rolled-back"
                    log("Rolled back to the previous version.")
                else:
                    status = "failed"
            else:
                status = "ok"
            if status == "ok" and artifact is not None:
                d = RELEASES_DIR / str(app_id)
                d.mkdir(mode=0o700, parents=True, exist_ok=True)
                name = f"{utc_now().strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.tar.gz"
                import shutil as _sh
                await asyncio.to_thread(_sh.copyfile, artifact, d / name)
                os.chmod(d / name, 0o600)
                artifact_release = (name, (d / name).stat().st_size)
        except DeployError as exc:
            log(f"FAILED: {exc}")
            status = "failed"
            message = message or str(exc)
        except Exception as exc:
            logging.exception("Deploy crashed")
            log(f"FAILED: {exc}")
            status = "failed"
        finally:
            if old_tree is not None:
                import shutil as _sh
                await asyncio.to_thread(_sh.rmtree, old_tree, True)
            if artifact is not None:
                with contextlib.suppress(OSError):
                    artifact.unlink()
            log(f"Finished: {status}")
            log.close()
            if status == "ok":
                _add_release(app_id, "rollback" if target is not None else "deploy", commit_sha, message,
                             artifact_release[0] if artifact_release else None,
                             artifact_release[1] if artifact_release else 0, by)
            # A rollback target spared above, or a lower keep_releases saved while
            # this ran (put_git leaves pruning to the deploy then).
            with contextlib.suppress(Exception):
                _prune_releases(app_id)
            _record(app_id, last_deploy_status=status, last_deploy_at=utc_now(),
                    last_deploy_commit=commit_sha if status == "ok" else None)
            if app_id in _state:
                _state[app_id].update(running=False, status=status, finished=utc_now().isoformat())
            row = _app(app_id)
            name = row.name if row else f"App {app_id}"
            audit_log(by, "app_deploy" if not target else "app_rollback", f"app '{name}' (id={app_id}): {status}")
            tail = "\n".join(log.lines[-8:])
            if row is not None and commit_sha:
                await report_commit_status(row, commit_sha, "success" if status == "ok" else "failure",
                                           {"ok": "Deployed", "rolled-back": "Deploy failed, rolled back"}.get(status, "Deploy failed"))
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
    auth_hdr = headers.get("authorization") or ""
    bearer = auth_hdr[7:].strip() if auth_hdr[:7].lower() == "bearer " else None
    for candidate in (headers.get("x-gitlab-token"), headers.get("x-venvy-secret"), bearer):
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
