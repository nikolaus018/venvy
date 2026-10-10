"""Startup checks for the host tools apps need (python3-venv, cargo)."""
import asyncio
import logging
import os
import shutil
import sys

from venvy.config import DATA_DIR


# ---------------------------------------------------------------------------
# Startup dependency checks
# ---------------------------------------------------------------------------

async def _ensure_python3_venv() -> None:
    """Verify that `python -m venv` works, otherwise auto-install."""
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

    minor = sys.version_info.minor
    packages_to_try = [f"python3.{minor}-venv", "python3-venv"]

    logging.warning(
        f"python3-venv is not available — attempting: "
        f"apt-get install -y {' '.join(packages_to_try)}"
    )

    for pkg in packages_to_try:
        try:
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            proc = await asyncio.create_subprocess_exec(
                "apt-get", "install", "-y", "--no-install-recommends", pkg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                logging.info(f"Successfully installed {pkg} via apt-get.")
                return
            
            proc = await asyncio.create_subprocess_exec(
                "sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", pkg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode == 0:
                logging.info(f"Successfully installed {pkg} via sudo apt-get.")
                return
        except Exception as exc:
            logging.warning(f"apt-get install {pkg} error: {exc}")

    logging.error(
        "Could not auto-install python3-venv. "
        f"Please run: sudo apt-get install -y python3.{minor}-venv\n"
        "App creation will fail until this is resolved."
    )


async def _ensure_rust() -> None:
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

    logging.warning("cargo is missing — attempting apt-get install -y cargo rustc")
    try:
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        proc = await asyncio.create_subprocess_exec(
            "apt-get", "install", "-y", "--no-install-recommends", "cargo", "rustc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            logging.info("Successfully installed cargo and rustc via apt-get.")
            return
        
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "apt-get", "install", "-y", "--no-install-recommends", "cargo", "rustc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode == 0:
            logging.info("Successfully installed cargo and rustc via sudo apt-get.")
            return
    except Exception as exc:
        logging.warning(f"Failed to install rust: {exc}")
