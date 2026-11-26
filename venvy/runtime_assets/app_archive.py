#!/usr/bin/env python3
"""Consistent copies of an app's files, made as the app's own user.

    app_archive.py sqlite ROOT OUT_DIR
        Every SQLite database under ROOT is copied to OUT_DIR/<same path>
        through SQLite's backup API (a consistent copy, even while the app
        writes to it, WAL included). Prints the relative paths as JSON.

    app_archive.py archive ROOT=ARCNAME [ROOT=ARCNAME ...]
        A tar.gz of the given folders on stdout, with every SQLite database
        in it replaced by such a copy. Symlinks are stored, never followed.
"""
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
import tempfile

MAGIC = b"SQLite format 3\x00"
SKIP_DIRS = {"node_modules", "target", ".git", "venv", ".venv", "__pycache__", ".cache", ".venvy"}
MAX_FILES = 200


def is_sqlite(path: str) -> bool:
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size < 512:
            return False
        with open(path, "rb") as f:
            return f.read(16) == MAGIC
    except OSError:
        return False


def find(root: str):
    found = []
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(dirpath, d))]
        for n in files:
            if n.endswith(("-wal", "-shm", "-journal")):
                continue
            p = os.path.join(dirpath, n)
            if is_sqlite(p):
                found.append(os.path.relpath(p, root))
                if len(found) >= MAX_FILES:
                    return found
    return found


def copy_db(src: str, dst: str) -> bool:
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        try:
            d = sqlite3.connect(dst)
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        return True
    except sqlite3.Error:
        try:
            os.unlink(dst)
        except OSError:
            pass
        return False


def cmd_sqlite(root: str, out: str) -> None:
    done = []
    for rel in find(root):
        if copy_db(os.path.join(root, rel), os.path.join(out, rel)):
            done.append(rel)
    print(json.dumps(done))


def cmd_archive(pairs) -> None:
    tmp = tempfile.mkdtemp(prefix="venvy-snap-")
    try:
        subs = {}
        for root, arc in pairs:
            for rel in find(root):
                dst = os.path.join(tmp, arc.replace("/", "_"), rel)
                if copy_db(os.path.join(root, rel), dst):
                    subs[os.path.join(root, rel)] = dst
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
            for root, arc in pairs:
                if not os.path.isdir(root):
                    continue
                for dirpath, dirs, files in os.walk(root, followlinks=False):
                    rel_dir = os.path.relpath(dirpath, root)
                    base = arc if rel_dir == "." else f"{arc}/{rel_dir}"
                    tar.add(dirpath, arcname=base, recursive=False)
                    for n in sorted(files) + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
                        p = os.path.join(dirpath, n)
                        if p in subs:
                            tar.add(subs[p], arcname=f"{base}/{n}", recursive=False)
                            continue
                        if any(p.endswith(sfx) and p[: -len(sfx)] in subs for sfx in ("-wal", "-shm", "-journal")):
                            continue
                        try:
                            st = os.lstat(p)
                            if stat.S_ISSOCK(st.st_mode):
                                continue
                            tar.add(p, arcname=f"{base}/{n}", recursive=False)
                        except (OSError, tarfile.TarError):
                            continue
                    dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(dirpath, d))]
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "sqlite":
        cmd_sqlite(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 3 and sys.argv[1] == "archive":
        pairs = []
        for a in sys.argv[2:]:
            root, _, arc = a.partition("=")
            pairs.append((root, arc))
        cmd_archive(pairs)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
