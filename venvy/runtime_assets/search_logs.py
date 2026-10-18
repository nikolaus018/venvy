#!/usr/bin/env python3
"""Searches an app's log files for Venvy. Runs as its own short-lived process
(`python3 -I search_logs.py`) because a user-supplied regular expression can
run for ever and, in Venvy's own process, would hold up everything else.

Reads one JSON object on stdin:  {"files": [...], "query": "...", "regex": bool,
"case": bool, "limit": int, "context": int}  and prints one JSON object:
{"matches": [{"file", "line", "text", "before": [...], "after": [...]}], "truncated": bool,
 "scanned_bytes": int}."""
import json
import re
import sys

MAX_LINE = 4000
MAX_SCAN_BYTES = 400 * 1024 * 1024


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
        name = path.rsplit("/", 1)[-1]
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            window = []
            pending = []        # matches still collecting their "after" lines
            for n, raw in enumerate(fh, 1):
                scanned += len(raw)
                line = raw.rstrip("\n")[:MAX_LINE]
                for m in pending:
                    if len(m["after"]) < ctx:
                        m["after"].append(line)
                pending = [m for m in pending if len(m["after"]) < ctx]
                if rx.search(line):
                    if len(matches) >= limit:
                        truncated = True
                        break
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
