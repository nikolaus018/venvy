"""Path containment and name sanitisation.

Every filesystem route in the API takes a user-supplied relative path. These
helpers are the single place that decides whether such a path is allowed to be
touched, so the containment rule can be reviewed — and tested — in one spot.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "PROTECTED_NAME_PREFIXES",
    "SKIPPED_TREE_NAMES",
    "is_safe_path",
    "is_safe_upload_name",
    "sanitize_slug",
    "validate_entrypoint",
]

#: Filenames the file browser refuses to create, rename over or delete. These
#: back the terminal jail; letting a user rewrite them would defeat it.
PROTECTED_NAME_PREFIXES = (".jailrc", ".jailsh", ".bashrc-venvy")

#: Directories hidden from the file tree — build artefacts and the app's own
#: virtualenv, which are large, uninteresting and never hand-edited.
SKIPPED_TREE_NAMES = frozenset(
    {
        "venv",
        "__pycache__",
        "logs",
        "node_modules",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "target",
    }
)

#: Characters permitted in an uploaded filename. Deliberately generous about
#: punctuation people actually use, but excludes path separators and control
#: characters entirely.
_SAFE_UPLOAD_NAME = re.compile(r"[A-Za-z0-9_.+\-\s()\[\]@#!~]+")

#: An entrypoint is a bare filename, never a path.
_SAFE_ENTRYPOINT = re.compile(r"[A-Za-z0-9_.\-]+")


def is_safe_path(root: Path, target: Path) -> bool:
    """Return True only if *target* resolves inside (or equal to) *root*.

    Both sides are fully resolved first, so symlinks pointing out of the app
    directory are rejected rather than followed.
    """
    try:
        root_resolved = root.resolve()
        target_resolved = target.resolve()
    except (ValueError, OSError):
        return False
    return target_resolved == root_resolved or root_resolved in target_resolved.parents


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve *relative* against *root*, raising if it escapes.

    :raises ValueError: if the result would fall outside *root*.
    """
    target = (root / relative).resolve()
    if not is_safe_path(root, target):
        raise ValueError("Path escapes the application directory.")
    return target


def sanitize_slug(name: str) -> str:
    """Derive a filesystem-safe directory name from a user-visible app name."""
    slug = re.sub(r"[\s\W-]+", "-", name.lower().strip()).strip("-")
    return slug or "app"


def is_safe_upload_name(filename: str) -> bool:
    """Return True if *filename* is a plain, safe basename to write to disk."""
    if not filename or "\0" in filename:
        return False
    if filename in (".", ".."):
        return False
    # A leading dot hides the file from the browser UI and is how the jail
    # scripts are named, so uploads never get to create one.
    if filename.startswith("."):
        return False
    return bool(_SAFE_UPLOAD_NAME.fullmatch(filename))


def is_protected_name(name: str) -> bool:
    """Return True if *name* is one of the jail-support files users may not touch."""
    return any(name.startswith(prefix) for prefix in PROTECTED_NAME_PREFIXES)


def validate_entrypoint(entrypoint: str) -> str:
    """Normalise and check a user-supplied entrypoint filename.

    :raises ValueError: if it is a path rather than a bare filename, or
        contains characters outside the safe set.
    """
    stripped = entrypoint.strip()
    if not stripped:
        return ""
    basename = Path(stripped).name
    if not basename or basename != stripped:
        raise ValueError("Entrypoint must be a plain filename (e.g. main.py), not a path.")
    if not _SAFE_ENTRYPOINT.fullmatch(basename):
        raise ValueError("Entrypoint filename contains invalid characters.")
    return basename
