import asyncio
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
from pathlib import Path
from typing import Dict, Set

from fastapi import WebSocket

try:
    import tomllib as _toml
except ImportError:
    # Fallback for Python < 3.11 (though Venvy recommends 3.11+)
    _toml = None


from fastapi import WebSocket
from sqlmodel import Session

# psutil is optional — provides richer process introspection
try:
    import psutil
except ImportError:
    psutil = None


from app.config import *
from app.database import *
from app.utils.process import *

# ---------------------------------------------------------------------------
# PTY Terminal
# ---------------------------------------------------------------------------


def _write_bashrc(jail_root: Path, venv_bin: Path, app_name_safe: str) -> Path:
    """Write a .bashrc for the app terminal with directory jail enforcement."""
    bashrc_path = jail_root / ".bashrc-venvy"
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


async def metrics_collector_task():
    """Background task to collect CPU/RAM usage for all active app processes."""
    if psutil is None:
        return
    proc_cache: Dict[int, psutil.Process] = {}  # type: ignore
    psutil.cpu_percent(interval=None)

    while True:
        try:
            global _host_cpu_percent
            _host_cpu_percent = psutil.cpu_percent(interval=None)

            current_app_ids = list(_active_app_pids.keys())
            active_pids = set(_active_app_pids.values())
            for pid in list(proc_cache.keys()):
                if pid not in active_pids:
                    proc_cache.pop(pid, None)

            for app_id in current_app_ids:
                pid = _active_app_pids.get(app_id)
                if not pid:
                    continue
                try:
                    if pid not in proc_cache:
                        p = psutil.Process(pid)
                        p.cpu_percent()
                        proc_cache[pid] = p

                    p = proc_cache[pid]
                    with p.oneshot():
                        cpu = p.cpu_percent()
                        mem = p.memory_info().rss
                        for child in p.children(recursive=True):
                            try:
                                with child.oneshot():
                                    cpu += child.cpu_percent()
                                    mem += child.memory_info().rss
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue

                        _app_metrics[app_id] = {"cpu": round(cpu, 1), "memory": mem}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    _app_metrics.pop(app_id, None)
                    proc_cache.pop(pid, None)
                except Exception:
                    pass
        except Exception as exc:
            logging.error(f"Metrics engine error: {exc}")
        await asyncio.sleep(2.5)


class TerminalManager:

    async def start_terminal(
        self,
        websocket: WebSocket,
        app_id: int,
        session: Session,
        user_id: int,
    ) -> None:
        db_app = session.get(App, app_id)
        if not db_app or db_app.user_id != user_id:
            await websocket.close(code=1008, reason="App not found or access denied.")
            return

        jail_root = Path(db_app.app_dir).resolve()
        venv_bin = Path(db_app.venv_path) / "bin"

        _app_name_safe = (
            re.sub(r"[^A-Za-z0-9_.+\-]", "-", db_app.name)[:24].strip("-") or "app"
        )

        try:
            bashrc_script = _write_bashrc(jail_root, venv_bin, _app_name_safe)
        except Exception as exc:
            logging.error(f"Failed to create bashrc: {exc}")
            await websocket.close()
            return

        _raw_env: Dict[str, str] = {
            "TERM": "xterm-256color",
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "USER": os.environ.get("USER", ""),
            "LOGNAME": os.environ.get("LOGNAME", ""),
            "SHELL": shutil.which("bash") or "/bin/bash",
            "PATH": f"{venv_bin}:/usr/local/bin:/usr/bin:/bin",
            "VIRTUAL_ENV": db_app.venv_path,
            "PYTHONUNBUFFERED": "1",
        }
        env: Dict[str, str] = {k: v for k, v in _raw_env.items() if v}
        try:
            _extra = json.loads(db_app.env_json or "{}")
            if isinstance(_extra, dict):
                for k, v in _extra.items():
                    env[str(k)] = str(v)
        except Exception:
            pass

        master_fd: int = -1
        slave_fd: int = -1
        pid: int = -1

        try:
            master_fd, slave_fd = pty.openpty()

            try:
                fcntl.ioctl(
                    master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 220, 0, 0)
                )
            except OSError:
                pass

            pid = os.fork()

            if pid == 0:
                try:
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

                    os.setsid()
                    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

                    os.dup2(slave_fd, 0)
                    os.dup2(slave_fd, 1)
                    os.dup2(slave_fd, 2)

                    _keep_fds: Set[int] = set()
                    try:
                        _my_pid = os.getpid()
                        _loopback_inodes: Set[int] = set()
                        for _tcp_file in ("/proc/net/tcp", "/proc/net/tcp6"):
                            try:
                                with open(_tcp_file) as _tf:
                                    for _line in _tf:
                                        _parts = _line.split()
                                        if len(_parts) < 10:
                                            continue
                                        _local = _parts[1]
                                        _hex_ip = _local.split(":")[0]
                                        if _hex_ip in (
                                            "7F000001",
                                            "0100007F",
                                            "00000001",
                                            "00000000000000000000000001000000",
                                        ):
                                            try:
                                                _loopback_inodes.add(int(_parts[9]))
                                            except (ValueError, IndexError):
                                                pass
                            except OSError:
                                pass

                        if _loopback_inodes:
                            _fd_dir = f"/proc/{_my_pid}/fd"
                            for _entry in os.listdir(_fd_dir):
                                try:
                                    _fd_int = int(_entry)
                                    if _fd_int < 3:
                                        continue
                                    _link = os.readlink(f"{_fd_dir}/{_entry}")
                                    if _link.startswith("socket:["):
                                        _inode = int(_link[8:-1])
                                        if _inode in _loopback_inodes:
                                            _keep_fds.add(_fd_int)
                                except (ValueError, OSError):
                                    pass
                    except Exception:
                        pass

                    try:
                        max_fd = os.sysconf("SC_OPEN_MAX")
                    except Exception:
                        max_fd = 4096
                    if not _keep_fds:
                        os.closerange(3, max_fd)
                    else:
                        _prev = 3
                        for _kfd in sorted(_keep_fds):
                            if _kfd > _prev:
                                os.closerange(_prev, _kfd)
                            _prev = _kfd + 1
                        if _prev < max_fd:
                            os.closerange(_prev, max_fd)

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
            stop = asyncio.Event()

            async def reader() -> None:
                import codecs

                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                try:
                    while not stop.is_set():
                        try:
                            data = await loop.run_in_executor(
                                None, os.read, master_fd, 4096
                            )
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
                        if data[0] == "\x00":
                            if len(data) > 4096:
                                continue
                            try:
                                msg = json.loads(data[1:])
                                if "cols" in msg and "rows" in msg:
                                    fcntl.ioctl(
                                        master_fd,
                                        termios.TIOCSWINSZ,
                                        struct.pack(
                                            "HHHH",
                                            int(msg["rows"]),
                                            int(msg["cols"]),
                                            0,
                                            0,
                                        ),
                                    )
                            except Exception:
                                pass
                            continue
                        try:
                            await loop.run_in_executor(
                                None, os.write, master_fd, data.encode("utf-8")
                            )
                        except (OSError, Exception):
                            break
                finally:
                    stop.set()

            t1 = asyncio.create_task(reader())
            t2 = asyncio.create_task(writer())
            try:
                await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.set()
                t1.cancel()
                t2.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)

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
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                await websocket.close()
            except Exception:
                pass


terminal_manager = TerminalManager()
