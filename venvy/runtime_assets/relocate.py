#!/usr/bin/env python3
"""Rewrite absolute paths in a virtualenv that was built in one folder and is
about to be moved to another: script shebangs and activate files in bin/,
.pth files and direct_url.json in site-packages. Run as the app's user.

    relocate.py VENV OLD_PREFIX NEW_PREFIX
"""
import os
import sys


def rewrite(path: str, old: bytes, new: bytes) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not os.path.isfile(path) or os.path.islink(path) or st.st_size > 4 * 1024 * 1024:
        return
    with open(path, "rb") as f:
        data = f.read()
    if old not in data or b"\0" in data[:4096]:
        return
    tmp = path + ".relocate-tmp"
    with open(tmp, "wb") as f:
        f.write(data.replace(old, new))
    os.chmod(tmp, st.st_mode & 0o7777)
    os.replace(tmp, path)


def main() -> None:
    venv, old, new = sys.argv[1], sys.argv[2].encode(), sys.argv[3].encode()
    bindir = os.path.join(venv, "bin")
    if os.path.isdir(bindir):
        for name in os.listdir(bindir):
            rewrite(os.path.join(bindir, name), old, new)
    for root, dirs, files in os.walk(os.path.join(venv, "lib")):
        if os.path.basename(root) == "site-packages":
            for name in files:
                if name.endswith(".pth"):
                    rewrite(os.path.join(root, name), old, new)
            for d in dirs:
                if d.endswith(".dist-info"):
                    rewrite(os.path.join(root, d, "direct_url.json"), old, new)
                    rewrite(os.path.join(root, d, "RECORD"), old, new)
            dirs[:] = [d for d in dirs if d.endswith(".dist-info")]
    rewrite(os.path.join(venv, "pyvenv.cfg"), old, new)


if __name__ == "__main__":
    main()
