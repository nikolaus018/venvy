"""Dependency management for an app's environment: pip for Python, cargo for Rust.

Package names are matched against a strict allowlist pattern before being
passed to the tool. Neither ``pip`` nor ``cargo`` is invoked through a shell,
so this is defence in depth rather than the only thing standing between a name
and command execution — but it also rejects nonsense early with a clear error
instead of a wall of tool output.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from venvy.core.audit import audit_log
from venvy.db.engine import get_session
from venvy.db.models import App, User
from venvy.schemas import PackageRequest
from venvy.security.dependencies import get_current_user, require_operator
from venvy.security.rate_limit import rate_limit_mutations

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/apps/{app_id}", tags=["packages"])

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

#: A PEP 508 name, optional extras, and an optional version specifier.
_PYTHON_SPEC = re.compile(r"[A-Za-z0-9_.+\-]+(\[[\w,]+\])?(\s*(==|>=|<=|~=|!=|>|<)\s*[^\s]+)?")
#: A bare distribution name, for uninstall.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_.+\-]+")
#: A crate name with an optional @version.
_CARGO_SPEC = re.compile(r"[A-Za-z0-9_.+\-]+(@[A-Za-z0-9_.+\-]+)?")

_LIST_TIMEOUT = 60
_INSTALL_TIMEOUT = 300
_UNINSTALL_TIMEOUT = 120
_CARGO_REMOVE_TIMEOUT = 60


def _get_app_or_404(session: Session, app_id: int) -> App:
    """Fetch an app or raise 404."""
    app = session.get(App, app_id)
    if not app:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


def _venv_python(app: App) -> Path:
    """Return the app's interpreter, raising 500 if its virtualenv is broken."""
    python = Path(app.venv_path) / "bin" / "python"
    if not python.exists():
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "The app's virtual environment is missing or incomplete.",
        )
    return python


def _venv_env(app: App) -> dict[str, str]:
    """Environment for a pip invocation targeting the app's virtualenv."""
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(app.venv_path)
    # Venvy's own interpreter settings would otherwise leak into the subprocess
    # and let pip resolve against the wrong site-packages.
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def _require_supported_runtime(app: App) -> None:
    """Reject runtimes with no package manager to talk to."""
    if app.runtime not in ("python", "rust"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Package management is only supported for Python and Rust apps.",
        )


def _validate(pattern: re.Pattern[str], value: str, label: str = "package name") -> str:
    """Return the trimmed value if it matches *pattern*, else raise 400."""
    cleaned = value.strip()
    if not pattern.fullmatch(cleaned):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {label}: {value!r}")
    return cleaned


async def _run(
    *command: str, timeout: int, env: dict[str, str] | None = None, cwd: str | None = None
) -> tuple[int, bytes, bytes]:
    """Run a command, returning ``(returncode, stdout, stderr)``."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"{command[0]} is not installed on this host."
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, f"`{command[0]}` timed out after {timeout}s."
        ) from exc
    return process.returncode or 0, stdout, stderr


def _read_cargo_dependencies(app: App) -> list[dict]:
    """Parse Cargo.toml and return its declared dependencies."""
    manifest = Path(app.app_dir) / "Cargo.toml"
    if not manifest.exists():
        return []
    if tomllib is None:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Reading Cargo.toml requires Python 3.11 or newer.",
        )

    try:
        with open(manifest, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.error("Could not parse %s: %s", manifest, exc)
        return []

    dependencies = {**data.get("dependencies", {}), **data.get("dev-dependencies", {})}
    return [
        {
            "name": name,
            # A dependency is either a bare version string or a table that may
            # carry one (path and git dependencies have none).
            "version": spec if isinstance(spec, str) else spec.get("version", "unknown"),
        }
        for name, spec in dependencies.items()
    ]


@router.get("/packages")
async def list_packages(
    app_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """List the packages installed in the app's environment."""
    app = _get_app_or_404(session, app_id)
    _require_supported_runtime(app)

    if app.runtime == "rust":
        return await asyncio.to_thread(_read_cargo_dependencies, app)

    python = _venv_python(app)
    code, stdout, _ = await _run(
        str(python),
        "-m",
        "pip",
        "list",
        "--format=json",
        timeout=_LIST_TIMEOUT,
        env=_venv_env(app),
    )
    if code != 0:
        return []
    try:
        return json.loads(stdout)
    except ValueError:
        logger.warning("pip list returned unparseable output for app %s", app_id)
        return []


@router.post("/packages")
async def install_package(
    app_id: int,
    request: PackageRequest,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Install a package into the app's environment."""
    app = _get_app_or_404(session, app_id)
    _require_supported_runtime(app)

    if app.runtime == "rust":
        name = _validate(_CARGO_SPEC, request.name, "crate name")
        manifest = Path(app.app_dir) / "Cargo.toml"
        code, _, stderr = await _run(
            "cargo",
            "add",
            name,
            "--manifest-path",
            str(manifest),
            timeout=_INSTALL_TIMEOUT,
        )
        if code != 0:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"cargo add failed: {stderr.decode(errors='replace')}",
            )
    else:
        name = _validate(_PYTHON_SPEC, request.name, "package specifier")
        python = _venv_python(app)
        code, _, stderr = await _run(
            str(python),
            "-m",
            "pip",
            "install",
            "--no-input",
            name,
            timeout=_INSTALL_TIMEOUT,
            env=_venv_env(app),
        )
        if code != 0:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Install failed: {stderr.decode(errors='replace')}",
            )

    audit_log(user.username, "package_install", f"'{app.name}' (id={app_id}): {name}")
    return {"ok": True}


@router.delete("/packages/{name}")
async def uninstall_package(
    app_id: int,
    name: str,
    session: Session = Depends(get_session),
    user: User = Depends(require_operator),
    _rate_limit: None = Depends(rate_limit_mutations),
):
    """Remove a package from the app's environment."""
    app = _get_app_or_404(session, app_id)
    _require_supported_runtime(app)
    clean_name = _validate(_PLAIN_NAME, name)

    if app.runtime == "rust":
        manifest = Path(app.app_dir) / "Cargo.toml"
        code, _, stderr = await _run(
            "cargo",
            "remove",
            clean_name,
            "--manifest-path",
            str(manifest),
            timeout=_CARGO_REMOVE_TIMEOUT,
        )
        if code != 0:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"cargo remove failed: {stderr.decode(errors='replace')}",
            )
    else:
        python = _venv_python(app)
        code, _, stderr = await _run(
            str(python),
            "-m",
            "pip",
            "uninstall",
            "-y",
            clean_name,
            timeout=_UNINSTALL_TIMEOUT,
            env=_venv_env(app),
        )
        if code != 0:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Uninstall failed: {stderr.decode(errors='replace')}",
            )

    audit_log(user.username, "package_uninstall", f"'{app.name}' (id={app_id}): {clean_name}")
    return {"ok": True}


def _collect_imports(root: Path) -> set[str]:
    """Return the top-level module names imported anywhere under *root*.

    Parses with :mod:`ast` rather than running the code — this must be safe on
    source that was just uploaded and never executed.
    """
    imported: set[str] = set()
    for source_file in root.rglob("*.py"):
        if SKIPPED_IMPORT_DIRS.intersection(source_file.parts):
            continue
        try:
            tree = ast.parse(source_file.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            # level > 0 is a relative import — a sibling module, not a
            # third-party package that could be missing.
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
    return imported


#: Directories excluded from the import scan.
SKIPPED_IMPORT_DIRS = frozenset({"venv", "__pycache__", ".git", "node_modules", "target"})


@router.get("/scan-imports")
async def scan_imports(
    app_id: int,
    session: Session = Depends(get_session),
    _user: User = Depends(get_current_user),
):
    """Report imports the app makes that are not installed in its environment.

    Powers the "missing dependencies" hint. Best-effort by nature: the import
    name and the distribution name often differ (``import yaml`` comes from
    ``PyYAML``), so the result is a prompt for the user rather than a
    dependency resolver.
    """
    app = _get_app_or_404(session, app_id)
    root = Path(app.app_dir)

    installed: set[str] = set()
    python = Path(app.venv_path) / "bin" / "python"
    if python.exists():
        code, stdout, _ = await _run(
            str(python),
            "-m",
            "pip",
            "list",
            "--format=json",
            timeout=_LIST_TIMEOUT,
            env=_venv_env(app),
        )
        if code == 0:
            try:
                for package in json.loads(stdout):
                    name = package["name"].lower()
                    # Distribution names normalise '-' and '_' interchangeably;
                    # record both so neither spelling shows as missing.
                    installed.update({name, name.replace("-", "_"), name.replace("_", "-")})
            except (ValueError, KeyError, TypeError):
                logger.warning("Could not parse pip list output for app %s", app_id)

    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    imported = await asyncio.to_thread(_collect_imports, root)

    missing = sorted(
        name
        for name in imported
        if name and name not in stdlib and name not in installed and not name.startswith("_")
    )
    return {"missing": missing, "installed_count": len(installed)}
