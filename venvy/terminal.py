"""The in-browser terminal: a PTY shell per WebSocket, run as the app's user."""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import secrets
import shlex
import signal
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket
from sqlmodel import Session

from venvy import shared_env
from venvy.config import TERMINAL_RC_DIR
from venvy.crypto import _try_decrypt_secret
from venvy.db import engine
from venvy.isolation import (
    _app_username,
    _as_app_user,
    _clean_root_env,
    _ensure_app_user,
    _exec_env_argv,
    _isolation_active,
    _prepare_exec_env,
    _resolve_system_bin,
    strip_dangerous_env_vars,
)
from venvy.models import App


# ---------------------------------------------------------------------------
# PTY Terminal
# ---------------------------------------------------------------------------

def _write_bashrc(app_id: int, jail_root: Path, venv_bin: Path, app_name_safe: str,
                  term_ids: Optional[tuple] = None) -> Path:
    """Write the terminal's rc file atomically. It lives in Venvy's own
    directory, not the app's: the app can't tamper with it, and Venvy never
    writes (as root, under per-app users) to a path the app controls. The cd
    "jail" in it is a convenience only — the real boundary is the per-app user,
    when enabled."""
    TERMINAL_RC_DIR.mkdir(mode=0o711, exist_ok=True)
    with contextlib.suppress(OSError):
        TERMINAL_RC_DIR.chmod(0o711)
    bashrc_path = TERMINAL_RC_DIR / f"app-{app_id}.bashrc"
    activate_quoted = shlex.quote(str(venv_bin / "activate"))
    jail_quoted = shlex.quote(str(jail_root))

    script = f"""#!/bin/bash
# Activate virtual environment
if [ -f {activate_quoted} ]; then source {activate_quoted}; fi

# ── Venvy Terminal Jail ───────────────────────────────────────────────────────
export JAIL_DIR={jail_quoted}

# Override cd to block navigation outside the app directory
function cd() {{
    local target jail_resolved
    jail_resolved="$(realpath -m "$JAIL_DIR" 2>/dev/null || echo "$JAIL_DIR")"
    if [ -z "$1" ]; then
        target="$jail_resolved"
    elif [[ "$1" == /* ]]; then
        target="$(realpath -m "$1" 2>/dev/null || echo "$1")"
    else
        target="$(realpath -m "$(builtin pwd)/$1" 2>/dev/null || echo "$(builtin pwd)/$1")"
    fi
    if [[ "$target" != "$jail_resolved" && "$target" != "$jail_resolved/"* ]]; then
        echo -e "\\e[31m✗ Access denied: cannot navigate outside the app directory.\\e[0m"
        return 1
    fi
    builtin cd "$target"
}}

# PROMPT_COMMAND: auto-correct cwd if it ever drifts outside the jail
function _venvy_jail_guard() {{
    local cwd jail_resolved
    cwd="$(builtin pwd -P 2>/dev/null)"
    jail_resolved="$(realpath -m "$JAIL_DIR" 2>/dev/null || echo "$JAIL_DIR")"
    if [[ "$cwd" != "$jail_resolved" && "$cwd" != "$jail_resolved/"* ]]; then
        builtin cd "$JAIL_DIR"
        echo -e "\\e[33m⚠ Returned to app directory (outside access blocked).\\e[0m"
    fi
}}
# The path inside the app's folder, "~" being the folder itself, for the
# prompt: \\w would spell out the whole data directory, half a phone screen.
function _venvy_where() {{
    _venvy_jail_guard
    local rest="${{PWD#"$JAIL_DIR"}}"
    [[ "$rest" == "$PWD" ]] && rest="$PWD" || rest="~$rest"
    VENVY_WHERE="$rest"
}}
PROMPT_COMMAND="_venvy_where"
# ─────────────────────────────────────────────────────────────────────────────

export PS1='\\[\\e[0;32m\\]{app_name_safe}\\[\\e[0m\\] \\[\\e[1;34m\\]${{VENVY_WHERE}}\\[\\e[0m\\]\\$ '
alias ls='ls --color=auto'
alias ll='ls -la'
"""
    tmp = TERMINAL_RC_DIR / f".app-{app_id}-{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
            f.flush()
            if term_ids:
                os.fchown(f.fileno(), 0, term_ids[1])
                os.fchmod(f.fileno(), 0o640)
            else:
                os.fchmod(f.fileno(), 0o644)
        os.replace(tmp, bashrc_path)
    except Exception as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        logging.error(f"Failed to write .bashrc: {exc}")
        raise
    return bashrc_path


_TERMINAL_RECHECK_SEC = float(os.getenv("VENVY_TERMINAL_RECHECK_SEC", "10"))


# Every open terminal is a forked shell and a PTY; a page that keeps opening
# them must run into a ceiling before the host runs out of either.
_MAX_TERMINALS = max(1, int(os.getenv("VENVY_MAX_TERMINALS", "64")))


class TerminalManager:

    def __init__(self) -> None:
        # app_id -> the stop event of each terminal open on that app.
        self._open: Dict[int, Set[asyncio.Event]] = {}

    def open_count(self) -> int:
        return sum(len(stops) for stops in self._open.values())

    async def close_for_app(self, app_id: Optional[int] = None, timeout: float = 5.0) -> None:
        """End the terminals open on *app_id* (all of them when None) and
        wait until their shells are gone. Used before an app's directory and
        Unix user are removed — a shell left running in there would keep
        both in use — and before Venvy shuts down or re-executes itself."""
        def targets() -> list:
            return [s for aid, stops in self._open.items() if app_id in (None, aid) for s in stops]

        for stop in targets():
            stop.set()
        deadline = time.monotonic() + timeout
        while targets() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def start_terminal(
        self,
        websocket: WebSocket,
        app_id: int,
        user_id: int,
        user_role: str,
        still_allowed=None,
        size: Optional[tuple] = None,
        first_input: Optional[str] = None,
    ) -> None:
        """Run a shell for *app_id* on *websocket* until either side closes.
        still_allowed() is polled while it is open; when it turns False the
        terminal is closed."""
        del user_id
        if user_role not in ("admin", "operator"):
            await websocket.close(code=1008, reason="Operator or admin privileges required for terminal access.")
            return
        if self.open_count() >= _MAX_TERMINALS:
            await websocket.close(code=1013, reason="Too many terminals are open — close one and try again.")
            return

        # Reserve the slot immediately (before any await) so concurrent
        # connections cannot exceed _MAX_TERMINALS and close_for_app() sees us.
        stop = asyncio.Event()
        self._open.setdefault(app_id, set()).add(stop)

        master_fd: int = -1
        slave_fd: int = -1
        proc: Optional[subprocess.Popen] = None

        try:
            # Read the app off the event loop and let go of the DB straight away.
            def _load_app_and_shared():
                with Session(engine) as session:
                    return session.get(App, app_id), shared_env.load()

            db_app, shared_vars = await asyncio.to_thread(_load_app_and_shared)
            if not db_app or stop.is_set():
                await websocket.close(code=1008, reason="App not found.")
                return

            jail_root = Path(db_app.app_dir).resolve()
            venv_bin = Path(db_app.venv_path) / "bin"
            _app_name_safe = re.sub(r"[^A-Za-z0-9_.+\-]", "-", db_app.name)[:24].strip("-") or "app"

            try:
                term_ids = await asyncio.to_thread(_ensure_app_user, db_app)
                bashrc_script = await asyncio.to_thread(
                    _write_bashrc, app_id, jail_root, venv_bin, _app_name_safe, term_ids
                )
            except Exception as exc:
                logging.error(f"Failed to create bashrc: {exc}")
                await websocket.close()
                return

            if stop.is_set():
                await websocket.close()
                return

            bash_exe = _resolve_system_bin("bash")
            base_path = f"{venv_bin}:/usr/local/bin:/usr/bin:/bin"
            _raw_env: Dict[str, str] = {
                "TERM": "xterm-256color",
                "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "HOME": os.environ.get("HOME", "/tmp"),
                "USER": os.environ.get("USER", ""),
                "LOGNAME": os.environ.get("LOGNAME", ""),
                "SHELL": bash_exe,
                "PATH": base_path,
                "VIRTUAL_ENV": db_app.venv_path,
                "PYTHONUNBUFFERED": "1",
            }
            env: Dict[str, str] = {k: v for k, v in _raw_env.items() if v}
            # Merge shared and app-specific variables first, then restore protected
            # identity/PATH/SHELL settings so app vars can't break or hijack them.
            env.update(shared_vars)
            env_ok, env_plain = _try_decrypt_secret(db_app.env_json)
            if not env_ok:
                with contextlib.suppress(Exception):
                    await websocket.send_text(
                        "\r\n\x1b[33m⚠ App environment variables could not be decrypted (.venvy_key mismatch).\x1b[0m\r\n"
                    )
            else:
                try:
                    _extra = json.loads(env_plain or "{}")
                    if isinstance(_extra, dict):
                        for k, v in _extra.items():
                            env[str(k)] = str(v)
                except Exception:
                    pass
            strip_dangerous_env_vars(env)
            if term_ids:
                env.update(HOME=str(jail_root), USER=_app_username(app_id), LOGNAME=_app_username(app_id))
            # Always keep venv_bin and system bins on PATH.
            existing_path = env.get("PATH", "")
            env["PATH"] = f"{base_path}:{existing_path}" if existing_path and existing_path != base_path else base_path
            env["SHELL"] = bash_exe
            env["VIRTUAL_ENV"] = db_app.venv_path

            master_fd, slave_fd = pty.openpty()

            rows, cols = size if size and 0 < size[0] < 1000 and 0 < size[1] < 1000 else (50, 220)
            with contextlib.suppress(OSError):
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

            # Spawn via subprocess.Popen (_posixsubprocess / C-level fork+exec)
            # + exec_env.py --ctty: zero Python code runs between fork and exec,
            # avoiding multi-threaded fork deadlocks, and root `setpriv` never
            # receives untrusted app environment variables.
            env_tf, env_fd = _prepare_exec_env(env)
            try:
                inner_argv = _exec_env_argv(env_fd, [bash_exe, "--rcfile", str(bashrc_script)], ctty=True)
                if _isolation_active():
                    spawn_argv = _as_app_user(db_app, inner_argv)
                    spawn_env = _clean_root_env()
                else:
                    spawn_argv = inner_argv
                    spawn_env = _clean_root_env()
                proc = subprocess.Popen(
                    spawn_argv,
                    cwd=str(jail_root),
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                    pass_fds=(env_fd,),
                    start_new_session=True,
                    env=spawn_env,
                )
            finally:
                env_tf.close()

            os.close(slave_fd)
            slave_fd = -1

            loop = asyncio.get_running_loop()

            # PTY I/O is non-blocking and driven by the event loop. Reading
            # with a blocking os.read() in the default executor parked one of
            # its few threads per open terminal for as long as it stayed open;
            # once they were all taken, every asyncio.to_thread() in Venvy
            # (app start/stop, uploads, backups) waited forever.
            os.set_blocking(master_fd, False)

            async def _wait_fd(add, remove) -> None:
                fut = loop.create_future()

                def _ready() -> None:
                    remove(master_fd)
                    if not fut.done():
                        fut.set_result(None)

                add(master_fd, _ready)
                try:
                    await fut
                finally:
                    remove(master_fd)

            async def _pty_write(payload: bytes) -> None:
                view = memoryview(payload)
                while view:
                    try:
                        view = view[os.write(master_fd, view):]
                    except BlockingIOError:
                        await _wait_fd(loop.add_writer, loop.remove_writer)

            async def reader() -> None:
                import codecs
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    while not stop.is_set():
                        try:
                            data = os.read(master_fd, 4096)
                        except BlockingIOError:
                            await _wait_fd(loop.add_reader, loop.remove_reader)
                            continue
                        except OSError:
                            break
                        if not data:
                            break
                        text = decoder.decode(data)
                        if not text:
                            continue
                        try:
                            await asyncio.wait_for(
                                websocket.send_text(text),
                                timeout=30.0,
                            )
                        except (asyncio.TimeoutError, Exception):
                            break
                finally:
                    stop.set()

            async def writer() -> None:
                try:
                    if first_input:
                        # Typed before the size arrived (see terminal_ws): not lost.
                        await _pty_write(first_input.encode("utf-8"))
                    while not stop.is_set():
                        try:
                            data = await asyncio.wait_for(
                                websocket.receive_text(), timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            continue
                        except Exception:
                            break
                        if not data:
                            continue
                        if data[0] == '\x00':
                            if len(data) > 4096:
                                continue
                            try:
                                msg = json.loads(data[1:])
                                if "cols" in msg and "rows" in msg:
                                    r_val = int(msg["rows"])
                                    c_val = int(msg["cols"])
                                    if 1 <= r_val < 1000 and 1 <= c_val < 1000:
                                        fcntl.ioctl(
                                            master_fd, termios.TIOCSWINSZ,
                                            struct.pack("HHHH", r_val, c_val, 0, 0),
                                        )
                            except Exception:
                                pass
                            continue
                        try:
                            await _pty_write(data.encode("utf-8"))
                        except (OSError, Exception):
                            break
                finally:
                    stop.set()

            async def watchdog() -> None:
                # A terminal is a root-of-the-app shell that used to stay open
                # for as long as the tab did: signing out, having the session
                # revoked by a password change, being deleted or the app being
                # deleted all left it usable. Re-check periodically.
                while still_allowed is not None:
                    await asyncio.sleep(_TERMINAL_RECHECK_SEC)
                    try:
                        allowed = await asyncio.to_thread(still_allowed)
                    except Exception:
                        continue   # e.g. DB briefly busy — not a reason to cut the user off
                    if not allowed:
                        with contextlib.suppress(Exception):
                            await websocket.send_text("\r\n\x1b[31m─── session ended ───\x1b[0m\r\n")
                        return
                await stop.wait()

            t1 = asyncio.create_task(reader())
            t2 = asyncio.create_task(writer())
            t3 = asyncio.create_task(watchdog())
            t4 = asyncio.create_task(stop.wait())   # close_for_app()
            tasks = [t1, t2, t3, t4]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.set()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

                # Close master_fd first so the shell receives immediate SIGHUP/EOF.
                if master_fd != -1:
                    with contextlib.suppress(OSError):
                        os.close(master_fd)
                    master_fd = -1

                if proc is not None and proc.poll() is None:
                    pid = proc.pid
                    def _sig_term(sig: int) -> None:
                        try:
                            if pid != os.getpgid(0):
                                os.killpg(pid, sig)
                            else:
                                os.kill(pid, sig)
                        except OSError:
                            with contextlib.suppress(OSError):
                                os.kill(pid, sig)

                    _sig_term(signal.SIGHUP)
                    for _ in range(30):
                        if proc.poll() is not None:
                            break
                        await asyncio.sleep(0.1)
                    if proc.poll() is None:
                        _sig_term(signal.SIGKILL)
                        with contextlib.suppress(Exception):
                            await loop.run_in_executor(None, proc.wait, 5)
                    proc = None

        except Exception as exc:
            logging.error(f"PTY execution error: {exc}")
        finally:
            for fd in (slave_fd, master_fd):
                if fd != -1:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait, 5)
            stops = self._open.get(app_id)
            if stops is not None:
                stops.discard(stop)
                if not stops:
                    self._open.pop(app_id, None)
            with contextlib.suppress(Exception):
                await websocket.close()


terminal_manager = TerminalManager()
