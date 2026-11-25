#!/usr/bin/env python3
"""Searches an app's log files for Venvy. Runs as its own short-lived process
(`python3 -I search_logs.py`) because a user-supplied regular expression can
run for ever and, in Venvy's own process, would hold up everything else.

Reads one JSON object on stdin:  {"files": [...], "query": "...", "regex": bool,
"case": bool, "limit": int, "context": int}  and prints one JSON object:
{"matches": [{"file", "line", "text", "before": [...], "after": [...]}], "truncated": bool,
 "scanned_bytes": int}."""
import json
import os
import re
import stat
import sys

MAX_LINE = 4000
MAX_LINE_BYTES = 64 * 1024
MAX_SCAN_BYTES = 400 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _iter_lines_bounded(fh):
    """Yield (byte_count, decoded_line) chunks without ever loading an unbounded
    newline-free file into memory."""
    pending = b""
    while True:
        chunk = fh.read(_CHUNK_BYTES)
        if not chunk:
            if pending:
                yield len(pending), pending.rstrip(b"\r\n").decode("utf-8", errors="replace")[:MAX_LINE]
            return
        pending += chunk
        while True:
            idx = pending.find(b"\n")
            if idx != -1:
                raw_line = pending[:idx + 1]
                pending = pending[idx + 1:]
                yield len(raw_line), raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")[:MAX_LINE]
            elif len(pending) >= MAX_LINE_BYTES:
                raw_line = pending[:MAX_LINE_BYTES]
                pending = pending[MAX_LINE_BYTES:]
                yield len(raw_line), raw_line.rstrip(b"\r\n").decode("utf-8", errors="replace")[:MAX_LINE]
            else:
                break


def main() -> None:
    req = json.load(sys.stdin)
    flags = 0 if req.get("case") else re.IGNORECASE
    pattern = req["query"] if req.get("regex") else re.escape(req["query"])
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        print(json.dumps({"error": f"Invalid regular expression: {exc}"}))
        return
    limit = max(1, min(int(req.get("limit", 200)), 2000))
    ctx = max(0, min(int(req.get("context", 0)), 5))
    matches, scanned, truncated = [], 0, False
    for path in req["files"]:
        name = os.path.basename(path)
        try:
            fd = os.open(path, _OPEN_FLAGS)
        except OSError:
            continue
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                os.close(fd)
                continue
            try:
                os.set_blocking(fd, True)
            except (OSError, AttributeError):
                pass
            fh = os.fdopen(fd, "rb")
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            continue
        with fh:
            window = []
            pending = []        # matches still collecting their "after" lines
            for n, (nbytes, line) in enumerate(_iter_lines_bounded(fh), 1):
                scanned += nbytes
                for m in pending:
                    if len(m["after"]) < ctx:
                        m["after"].append(line)
                pending = [m for m in pending if len(m["after"]) < ctx]
                if truncated:
                    if not pending or scanned > MAX_SCAN_BYTES:
                        break
                    continue
                if rx.search(line):
                    if len(matches) >= limit:
                        truncated = True
                        if not pending:
                            break
                        continue
                    m = {"file": name, "line": n, "text": line, "before": window[-ctx:] if ctx else [], "after": []}
                    matches.append(m)
                    if ctx:
                        pending.append(m)
                if ctx:
                    window.append(line)
                    if len(window) > ctx:
                        window.pop(0)
                if scanned > MAX_SCAN_BYTES:
                    truncated = True
                    break
        if truncated:
            break
    print(json.dumps({"matches": matches, "truncated": truncated, "scanned_bytes": scanned}))


if __name__ == "__main__":
    main()
