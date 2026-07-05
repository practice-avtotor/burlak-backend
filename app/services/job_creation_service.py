from typing import Any

import aiosqlite

from app.core.exceptions import JobCreationError
from app.db.async_repository import create_job, get_job


class JobCreationService:
    """Creates a new job and validates the result."""

    @staticmethod
    async def create(
        db: aiosqlite.Connection, mode: str = "heuristic"
    ) -> dict[str, Any]:
        created = await create_job(db, mode=mode)
        job = await get_job(db, created["id"])
        if job is None:
            raise JobCreationError(f"Failed to create job {created['id']} in the database")
        # session_token is already in the row (get_job does SELECT *)
        return job
