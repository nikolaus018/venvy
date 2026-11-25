#!/usr/bin/env python3
"""Apply a JSON environment received on an inherited file descriptor, then exec.

Used after privilege drop (`setpriv --reuid=... --regid=... --no-new-privs`) so
untrusted app environment variables never enter root-privileged wrappers such as
`systemd-run` or `setpriv`. With `--ctty`, also makes stdin the controlling
terminal (used by the browser PTY terminal so `subprocess.Popen` never needs
Python code between `fork()` and `exec()`).
"""
import fcntl
import json
import os
import signal
import sys
import termios


def main() -> None:
    args = sys.argv[1:]
    ctty = False
    if args and args[0] == "--ctty":
        ctty = True
        args = args[1:]
    if len(args) < 2:
        sys.exit(2)
    env_fd = int(args[0])
    argv = args[1:]

    with os.fdopen(env_fd, "rb") as fh:
        raw = fh.read()
    env = {str(k): str(v) for k, v in json.loads(raw.decode("utf-8")).items()}

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    if ctty:
        try:
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        except OSError:
            pass

    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    main()
