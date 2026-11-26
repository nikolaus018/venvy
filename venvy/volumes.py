"""Folders shared between apps.

A volume is DATA_DIR/volumes/<name>. Apps attached to it see its path in
$VOLUME_<NAME>. Under per-app users, access is given with POSIX ACLs (the
volume is root-owned, mode 0770, and each attached app's user gets an ACL
entry — rwx, or r-x for read-only — inherited by new files), so detaching an
app takes its access away again. Apps that run as systemd services also get
the volume mounted read-only when attached read-only.
"""
import contextlib
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

from sqlmodel import Session, select

from venvy.config import DATA_DIR
from venvy.db import engine
from venvy.models import AppVolume, Volume

VOLUMES_DIR = DATA_DIR / "volumes"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9\-]{0,40}")


def path_of(name: str) -> Path:
    return VOLUMES_DIR / name


def env_name(name: str) -> str:
    return "VOLUME_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def create_dir(name: str) -> Path:
    VOLUMES_DIR.mkdir(mode=0o711, exist_ok=True)
    with contextlib.suppress(OSError):
        VOLUMES_DIR.chmod(0o711)
    p = path_of(name)
    p.mkdir(mode=0o770, exist_ok=True)
    with contextlib.suppress(OSError):
        p.chmod(0o770)
    return p


def _attached(volume_id: int) -> List[Tuple[int, bool]]:
    with Session(engine) as s:
        return [(r.app_id, r.read_only) for r in s.exec(select(AppVolume).where(AppVolume.volume_id == volume_id)).all()]


def apply_acl(volume: Volume) -> None:
    """Make the volume's ACL list exactly the apps attached to it."""
    from venvy.isolation import _app_username, _isolation_active, _resolve_system_bin
    p = create_dir(volume.name)
    if not _isolation_active():
        return
    setfacl = shutil.which("setfacl", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if not setfacl:
        raise RuntimeError("setfacl is not installed (apt-get install acl): volumes can't be shared under per-app users.")
    cmds = [[setfacl, "-R", "-b", str(p)]]
    for app_id, ro in _attached(volume.id):
        perm = "rX" if ro else "rwX"
        user = _app_username(app_id)
        cmds.append([setfacl, "-R", "-m", f"u:{user}:{perm}", str(p)])
        cmds.append([setfacl, "-R", "-d", "-m", f"u:{user}:{perm}", str(p)])
    for argv in cmds:
        r = subprocess.run(argv, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"setfacl failed: {r.stderr.decode(errors='replace').strip()}")
    # Traversal down to it, for the attached users.
    with contextlib.suppress(OSError):
        DATA_DIR.chmod(DATA_DIR.stat().st_mode | 0o011)


def mounts(owner_id: int) -> List[Tuple[Path, bool]]:
    with Session(engine) as s:
        out = []
        for av in s.exec(select(AppVolume).where(AppVolume.app_id == owner_id)).all():
            v = s.get(Volume, av.volume_id)
            if v is not None:
                out.append((path_of(v.name), av.read_only))
        return out


def volume_env(owner_id: int) -> Dict[str, str]:
    with Session(engine) as s:
        out = {}
        for av in s.exec(select(AppVolume).where(AppVolume.app_id == owner_id)).all():
            v = s.get(Volume, av.volume_id)
            if v is not None:
                out[env_name(v.name)] = str(path_of(v.name))
        return out


def remove_dir(name: str) -> None:
    p = path_of(name)
    if p.is_dir() and not p.is_symlink() and VOLUMES_DIR in p.parents:
        shutil.rmtree(p, ignore_errors=True)
