"""Startup checks for the host tools apps need (python3-venv, cargo).

These only report. Installing system packages is the installer's job
(install.sh) — a web server should not be running apt-get on its own.
"""
import asyncio
import logging
import shutil
import sys

from venvy.config import DATA_DIR


async def _check_python3_venv() -> None:
    """Log whether `python3 -m venv` works; Python apps can't be created without it."""
    test_dir = DATA_DIR / ".venv_probe"
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", "-m", "venv", "--without-pip", str(test_dir),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            logging.info("python3-venv: OK")
            return
        err_text = stderr.decode(errors="replace").strip()
        logging.warning(f"python3-venv probe failed (rc={proc.returncode}): {err_text}")
    except Exception as exc:
        logging.warning(f"python3-venv probe error: {exc}")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)

    logging.error(
        "python3-venv is not available — creating Python apps will fail. "
        f"Install it with: sudo apt-get install -y python3.{sys.version_info.minor}-venv"
    )


async def _check_rust() -> None:
    """Log whether cargo is available; only Rust apps need it."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "cargo", "-V",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            logging.info("cargo: OK")
            return
    except Exception:
        pass
    logging.info(
        "cargo is not installed — Rust apps are unavailable. "
        "To enable them: sudo apt-get install -y cargo rustc build-essential"
    )
