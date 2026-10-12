"""The in-browser terminal: a PTY shell per WebSocket, run as the app's user."""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import shutil
import signal
import struct
import termios
import time
from pathlib import Path
from typing import Dict, Optional, Set

from fastapi import WebSocket
from sqlmodel import Session

from venvy.config import TERMINAL_RC_DIR
from venvy.crypto import _decrypt_secret
from venvy.db import engine
from venvy.isolation import _app_username, _ensure_app_user
from venvy.models import App


# ---------------------------------------------------------------------------
# PTY Terminal
# ---------------------------------------------------------------------------

def _write_bashrc(app_id: int, jail_root: Path, venv_bin: Path, app_name_safe: str) -> Path:
    """Write the terminal's rc file. It lives in Venvy's own directory, not the
    app's: the app can't tamper with it, and Venvy never writes (as root,
    under per-app users) to a path the app controls. The cd "jail" in it is a
    convenience only — the real boundary is the per-app user, when enabled."""
    TERMINAL_RC_DIR.mkdir(mode=0o755, exist_ok=True)
    bashrc_path = TERMINAL_RC_DIR / f"app-{app_id}.bashrc"
    activate_script = venv_bin / "activate"
    jail_dir = str(jail_root)

    script = f"""#!/bin/bash
# Activate virtual environment
if [ -f "{activate_script}" ]; then source "{activate_script}"; fi

# ── Venvy Terminal Jail ───────────────────────────────────────────────────────
export JAIL_DIR="{jail_dir}"

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
PROMPT_COMMAND="_venvy_jail_guard"
# ─────────────────────────────────────────────────────────────────────────────

export PS1='\\[\\e[0;32m\\]{app_name_safe}\\[\\e[0m\\] \\[\\e[1;34m\\]\\w\\[\\e[0m\\]\\$ '
alias ls='ls --color=auto'
alias ll='ls -la'
"""
    try:
        bashrc_path.write_text(script, encoding="utf-8")
        bashrc_path.chmod(0o644)
    except Exception as exc:
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
    ) -> None:
        """Run a shell for *app_id* on *websocket* until either side closes.
        still_allowed() is polled while it is open; when it turns False the
        terminal is closed."""
        if user_role not in ("admin", "operator"):
            await websocket.close(code=1008, reason="Operator or admin privileges required for terminal access.")
            return
        # Read the app and let go of the DB straight away — see terminal_ws().
        with Session(engine) as session:
            db_app = session.get(App, app_id)
        if not db_app:
            await websocket.close(code=1008, reason="App not found.")
            return
        if self.open_count() >= _MAX_TERMINALS:
            await websocket.close(code=1013, reason="Too many terminals are open — close one and try again.")
            return

        jail_root = Path(db_app.app_dir).resolve()
        venv_bin  = Path(db_app.venv_path) / "bin"

        _app_name_safe = re.sub(r"[^A-Za-z0-9_.+\-]", "-", db_app.name)[:24].strip("-") or "app"

        try:
            bashrc_script = _write_bashrc(app_id, jail_root, venv_bin, _app_name_safe)
            term_ids = await asyncio.to_thread(_ensure_app_user, db_app)
        except Exception as exc:
            logging.error(f"Failed to create bashrc: {exc}")
            await websocket.close()
            return

        _raw_env: Dict[str, str] = {
            "TERM":             "xterm-256color",
            "LANG":             os.environ.get("LANG", "en_US.UTF-8"),
            "HOME":             os.environ.get("HOME", "/tmp"),
            "USER":             os.environ.get("USER", ""),
            "LOGNAME":          os.environ.get("LOGNAME", ""),
            "SHELL":            shutil.which("bash") or "/bin/bash",
            "PATH":             f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "VIRTUAL_ENV":      db_app.venv_path,
            "PYTHONUNBUFFERED": "1",
        }
        if term_ids:
            _raw_env.update(HOME=str(jail_root), USER=_app_username(app_id), LOGNAME=_app_username(app_id))
        env: Dict[str, str] = {k: v for k, v in _raw_env.items() if v}
        try:
            _extra = json.loads(_decrypt_secret(db_app.env_json) or "{}")
            if isinstance(_extra, dict):
                for k, v in _extra.items():
                    env[str(k)] = str(v)
        except Exception:
            pass

        master_fd: int = -1
        slave_fd:  int = -1
        pid:       int = -1

        stop = asyncio.Event()
        self._open.setdefault(app_id, set()).add(stop)

        try:
            master_fd, slave_fd = pty.openpty()

            try:
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 50, 220, 0, 0))
            except OSError:
                pass

            pid = os.fork()

            if pid == 0:
                try:
                    signal.signal(signal.SIGINT,  signal.SIG_DFL)
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

                    os.setsid()
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

                    os.dup2(slave_fd, 0)
                    os.dup2(slave_fd, 1)
                    os.dup2(slave_fd, 2)

                    # Close everything else inherited from Venvy — its listening
                    # socket, other users' HTTP connections, the DB. A shell (or
                    # anything started from it) must not be able to accept()
                    # Venvy's traffic or keep its port bound after it exits.
                    try:
                        max_fd = os.sysconf("SC_OPEN_MAX")
                    except Exception:
                        max_fd = 4096
                    os.closerange(3, max_fd)

                    if term_ids:
                        # Drop to the app's own user before anything runs.
                        os.setgroups([])
                        os.setgid(term_ids[1])
                        os.setuid(term_ids[0])

                    os.chdir(str(jail_root))
                    bash_exe = env.get("SHELL", "/bin/bash")
                    os.execvpe(
                        bash_exe,
                        [bash_exe, "--rcfile", str(bashrc_script)],
                        env,
                    )
                except Exception:
                    pass
                os._exit(1)

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
                                    fcntl.ioctl(
                                        master_fd, termios.TIOCSWINSZ,
                                        struct.pack("HHHH",
                                                    int(msg["rows"]), int(msg["cols"]), 0, 0),
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
                
                if pid > 0:
                    try:
                        os.kill(pid, signal.SIGHUP)
                    except OSError:
                        pass
                    async def _reap() -> None:
                        for _ in range(30):
                            try:
                                wpid, _ = os.waitpid(pid, os.WNOHANG)
                                if wpid != 0:
                                    return
                            except ChildProcessError:
                                return
                            await asyncio.sleep(0.1)
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except OSError:
                            pass
                        try:
                            await loop.run_in_executor(None, os.waitpid, pid, 0)
                        except Exception:
                            pass
                    await _reap()
                    # Reaped: the number may be handed to an unrelated process
                    # now, so the fallback kill below must not use it.
                    pid = -1

                if master_fd != -1:
                    try:
                        os.close(master_fd)
                    except OSError:
                        pass
                    master_fd = -1

        except Exception as exc:
            logging.error(f"PTY execution error: {exc}")
        finally:
            for fd in (slave_fd, master_fd):
                if fd != -1:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            if pid > 0:
                # Only reached when setup failed before the normal teardown.
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(os.waitpid, pid, 0)
            stops = self._open.get(app_id)
            if stops is not None:
                stops.discard(stop)
                if not stops:
                    self._open.pop(app_id, None)
            try:
                await websocket.close()
            except Exception:
                pass


terminal_manager = TerminalManager()
