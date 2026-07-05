"""Celery task: cleanup — periodic removal of stale jobs.

Runs every hour via Celery Beat.  Removes jobs older than 24 hours:
  1. Deletes storage files (``/data/{job_id}/`` directory)
  2. Deletes DB records (jobs + cards rows)
  3. Invalidates Redis cache for each deleted job

This prevents disk and database from growing indefinitely.
"""

from __future__ import annotations

import logging

from celery import Task  # type: ignore[import-untyped]

from app.core.storage import cleanup_job
from app.db import sync_repository
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Jobs older than this many hours are eligible for cleanup.
MAX_AGE_HOURS = 24


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    name="app.worker.tasks.cleanup.cleanup_old_jobs",
    max_retries=1,
    retry_backoff=True,
)
def cleanup_old_jobs(self: Task, max_age_hours: int = MAX_AGE_HOURS) -> dict[str, int]:
    """Delete jobs older than *max_age_hours*.

    For each stale job:
      1. Removes the storage directory (``cleanup_job``)
      2. Deletes the job + cards rows from the DB (``delete_job``)
      3. Invalidates the Redis cache for the job

    Returns a summary dict with the count of deleted jobs and any errors.
    """
    logger.info("Starting cleanup of jobs older than %d hours", max_age_hours)

    old_job_ids = sync_repository.get_old_job_ids(max_age_hours=max_age_hours)
    if not old_job_ids:
        logger.info("No stale jobs found for cleanup")
        return {"deleted": 0, "errors": 0}

    deleted = 0
    errors = 0

    for job_id in old_job_ids:
        try:
            # 1. Remove storage files (best-effort)
            try:
                cleanup_job(job_id)
            except Exception as exc:
                logger.warning("Failed to cleanup storage for job %d: %s", job_id, exc)

            # 2. Delete DB records
            sync_repository.delete_job(job_id)

            # 3. Invalidate Redis cache (best-effort, sync client)
            try:
                from app.core.redis import get_sync_redis
                get_sync_redis().delete(f"job:status:{job_id}")
            except Exception:
                pass  # Cache invalidation is best-effort

            deleted += 1
            logger.info("Cleaned up stale job %d", job_id)
        except Exception as exc:
            errors += 1
            logger.error("Failed to delete stale job %d: %s", job_id, exc, exc_info=True)

    logger.info(
        "Cleanup complete: deleted %d jobs, %d errors (out of %d candidates)",
        deleted, errors, len(old_job_ids),
    )
    return {"deleted": deleted, "errors": errors}
