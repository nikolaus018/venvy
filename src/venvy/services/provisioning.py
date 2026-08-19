"""Startup checks for the toolchains Venvy needs to build apps.

Venvy needs ``python3 -m venv`` to create Python apps and ``cargo`` to build
Rust ones. Both are probed at startup and the result is reported clearly.

Installing them is **opt-in**. A server process that silently runs ``apt-get
install`` as a side effect of booting is surprising, needs root, and can fail
halfway on a host whose package state is not Venvy's business — so by default a
missing toolchain produces an actionable error telling the operator what to
run. Set ``VENVY_AUTO_INSTALL_SYSTEM_DEPS=1`` to opt in to the old behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys

from venvy.config import settings

__all__ = ["check_python_venv", "check_rust", "run_startup_checks"]

logger = logging.getLogger(__name__)

_VENV_PROBE_TIMEOUT = 30
_CARGO_PROBE_TIMEOUT = 5
_INSTALL_TIMEOUT = 120


async def _run(*command: str, timeout: int, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run *command*, returning ``(returncode, stderr)``.

    A missing executable or a timeout is reported as a non-zero code rather
    than raised, so probing never takes the startup sequence down.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (OSError, FileNotFoundError) as exc:
        return 127, str(exc)

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        return 124, f"timed out after {timeout}s"
    return process.returncode or 0, stderr.decode(errors="replace").strip()


async def _apt_install(*packages: str) -> bool:
    """Try to install *packages*, with and then without sudo. Returns success."""
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    base = ("apt-get", "install", "-y", "--no-install-recommends", *packages)

    for command in (base, ("sudo", "-n", *base)):
        code, stderr = await _run(*command, timeout=_INSTALL_TIMEOUT, env=env)
        if code == 0:
            logger.info("Installed %s via `%s`.", " ".join(packages), command[0])
            return True
        logger.debug("`%s` failed: %s", " ".join(command), stderr)
    return False


async def check_python_venv() -> bool:
    """Verify that ``python3 -m venv`` works. Returns True if usable."""
    probe_dir = settings.data_dir / ".venv_probe"
    try:
        code, stderr = await _run(
            "python3",
            "-m",
            "venv",
            "--without-pip",
            str(probe_dir),
            timeout=_VENV_PROBE_TIMEOUT,
        )
        if code == 0:
            logger.info("python3-venv: OK")
            return True
        logger.warning("python3-venv probe failed (rc=%s): %s", code, stderr)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)

    package = f"python3.{sys.version_info.minor}-venv"
    if settings.auto_install_system_deps:
        logger.warning("Attempting to install %s…", package)
        if await _apt_install(package) or await _apt_install("python3-venv"):
            return True

    logger.error(
        "python3-venv is not available — creating Python apps will fail.\n"
        "  Fix it with: sudo apt-get install -y %s\n"
        "  (or set VENVY_AUTO_INSTALL_SYSTEM_DEPS=1 to let Venvy try automatically)",
        package,
    )
    return False


async def check_rust() -> bool:
    """Verify that ``cargo`` is available. Returns True if usable."""
    code, _ = await _run("cargo", "-V", timeout=_CARGO_PROBE_TIMEOUT)
    if code == 0:
        logger.info("cargo: OK")
        return True

    if settings.auto_install_system_deps:
        logger.warning("cargo is missing — attempting to install cargo and rustc…")
        if await _apt_install("cargo", "rustc"):
            return True

    logger.warning(
        "cargo is not available — Rust apps cannot be built.\n"
        "  Fix it with: sudo apt-get install -y cargo rustc  (or install rustup)\n"
        "  Python apps are unaffected."
    )
    return False


async def run_startup_checks() -> dict[str, bool]:
    """Probe every toolchain, returning a name → availability map."""
    python_ok, rust_ok = await asyncio.gather(check_python_venv(), check_rust())
    return {"python": python_ok, "rust": rust_ok}
