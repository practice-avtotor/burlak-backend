"""Shared FastAPI dependencies for session-based job ownership verification.

Each job is created with a random ``session_token`` (see ``async_repository.create_job``).
All job-scoped endpoints (upload chunks, start processing, cancel, download results,
stream progress) must pass ``Depends(verify_job_session)`` which validates the
``X-Session-Token`` header against the token stored in the database.

This prevents users on the same local network from interacting with
each other's jobs — a critical isolation requirement for multi-user
factory-floor deployments.
"""

from __future__ import annotations

import secrets

import aiosqlite
from fastapi import Depends, Header

from app.core.exceptions import JobForbiddenError, JobNotFoundError
from app.db.async_repository import get_job
from app.db.database import get_async_db


async def _verify_token_sync(
    db: aiosqlite.Connection,
    job_id: int,
    provided_token: str | None,
) -> None:
    """Core session-token verification logic (shared by header and query-param paths).

    Raises:
        JobNotFoundError: Job does not exist.
        JobForbiddenError: Token is missing, empty, or does not match.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} not found")

    stored_token = job.get("session_token", "")
    if not stored_token:
        # Jobs created before the migration have no token — reject for safety
        raise JobForbiddenError(f"Job {job_id} has no session token (legacy job)")

    if not provided_token or not secrets.compare_digest(provided_token, stored_token):
        raise JobForbiddenError(f"Access denied to job {job_id}: invalid session token")


async def verify_job_session(
    job_id: int,
    x_session_token: str | None = Header(None, alias="X-Session-Token"),
    db: aiosqlite.Connection = Depends(get_async_db),
) -> None:
    """Verify that the request's session token matches the job's token.

    The header is optional at the FastAPI level so that non-existent jobs
    return 404 (not 422 "missing header").  If the job exists but the token
    is missing or wrong, 403 is returned.

    Raises:
        JobNotFoundError: Job does not exist.
        JobForbiddenError: Token does not match (wrong user or tampered request).
    """
    await _verify_token_sync(db, job_id, x_session_token)
