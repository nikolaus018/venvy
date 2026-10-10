"""Background jobs and long-lived asyncio tasks."""
import asyncio
import logging
import secrets
import time
from typing import Dict, Set

from fastapi import HTTPException

from venvy.models import User


# Track strongly-referenced background tasks to avoid garbage collection midway
_running_tasks: Set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------
# Creating an app (venv + pip), installing packages and building/restoring
# backups can take minutes. Cloudflare cuts proxied requests off at 100 s, so
# these endpoints start a job and answer {"job_id"} at once; the client polls
# GET /api/jobs/{id}.
_jobs: Dict[str, dict] = {}


_JOB_TTL_SEC = 3600


def _start_job(user: "User", kind: str, fn) -> dict:
    """Run coroutine function *fn* in the background; return {"job_id"}."""
    now = time.time()
    for jid in [j for j, v in _jobs.items() if v["status"] != "running" and now - v["updated"] > _JOB_TTL_SEC]:
        _jobs.pop(jid, None)
    job_id = secrets.token_hex(16)
    job = {"id": job_id, "kind": kind, "user_id": user.id, "status": "running",
           "result": None, "error": None, "code": None, "created": now, "updated": now}
    _jobs[job_id] = job

    async def _run():
        try:
            job["result"] = await fn()
            job["status"] = "done"
        except HTTPException as exc:
            job.update(status="error", error=exc.detail, code=exc.status_code)
        except Exception as exc:
            logging.error(f"Job {kind} failed: {exc}")
            job.update(status="error", error=str(exc) or exc.__class__.__name__, code=500)
        finally:
            job["updated"] = time.time()
            _running_tasks.discard(asyncio.current_task())

    t = asyncio.create_task(_run())
    _running_tasks.add(t)
    return {"job_id": job_id}
