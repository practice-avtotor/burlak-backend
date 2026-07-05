import logging

import aiosqlite

from app.core.exceptions import JobNotFoundError, JobStateError
from app.db.async_repository import get_job, try_start_processing
from app.db import sync_repository
from app.services.cache_service import invalidate_job_cache

logger = logging.getLogger(__name__)


class JobProcessingService:
    """Coordinates the job processing lifecycle.

    Responsibilities are split into two phases:
    1. transition_to_processing — atomically validate and persist the new state
    2. dispatch_processing      — trigger asynchronous processing task
    """

    @staticmethod
    async def transition_to_processing(
        db: aiosqlite.Connection,
        job_id: int,
    ) -> dict[str, str]:
        """Atomically validate preconditions and transition job status to 'processing'.

        If preconditions are not met, raises corresponding error (404 or 409).
        Invalidates the job status cache after mutation.

        Returns:
            The new status and stage written to the database.
        """
        success = await try_start_processing(db, job_id)
        if not success:
            job = await get_job(db, job_id)
            if job is None:
                raise JobNotFoundError(f"Job {job_id} not found")
            # Allow restart from 'error' or 'done' status (for retry)
            if job["status"] in ("error", "done"):
                from app.db.async_repository import reset_job_for_retry
                await reset_job_for_retry(db, job_id)
                await invalidate_job_cache(job_id)
                return {"status": "processing", "stage": "unpacking"}
            if job["status"] != "awaiting_upload":
                raise JobStateError(
                    f"Job {job_id} is in status '{job['status']}', expected 'awaiting_upload'"
                )
            if not job["bom_uploaded"] or not job["archive_uploaded"]:
                raise JobStateError(
                    f"Job {job_id} cannot be started: BOM and archive must be fully uploaded"
                )
            raise JobStateError(f"Job {job_id} cannot be started")
        await invalidate_job_cache(job_id)
        return {"status": "processing", "stage": "unpacking"}

    @staticmethod
    def dispatch_processing(job_id: int, mode: str = "ml") -> None:
        """Trigger the appropriate processing task based on job mode.

        - mode='heuristic': single monolithic task using burlak_parser
        - mode='ml': existing multi-step ML pipeline (unpack → analyze → process → aggregate → package)

        This is intentionally a sync method — it only enqueues work
        without awaiting the result.  The Celery task ID is persisted
        to the database so the job can be cancelled later.
        """
        if mode == "ml":
            from app.worker.tasks.unpack import unpack
            result = unpack.delay(job_id)
        else:
            from app.worker.tasks.process_heuristic import process_heuristic
            result = process_heuristic.delay(job_id)
        # Persist the Celery task ID for cancellation support (best-effort)
        if result and result.id:
            try:
                sync_repository.set_celery_task_id(job_id, result.id)
            except Exception as exc:
                logger.warning(
                    "Failed to store celery task ID for job %d: %s", job_id, exc,
                )

    @staticmethod
    async def cancel_job(db: aiosqlite.Connection, job_id: int) -> dict[str, str]:
        """Cancel a running job by revoking its Celery task and cleaning up.

        1. Revokes the Celery task (if a task_id is stored)
        2. Marks the job as 'cancelled' in the database
        3. Cleans up storage files for the job
        4. Invalidates the job status cache

        Returns:
            The new status ('cancelled') and the revoked task_id (or None).
        """
        job = await get_job(db, job_id)
        if job is None:
            raise JobNotFoundError(f"Job {job_id} not found")

        if job["status"] != "processing":
            raise JobStateError(
                f"Job {job_id} is in status '{job['status']}', can only cancel 'processing' jobs"
            )

        task_id = job.get("celery_task_id")

        # 1. Revoke the Celery task (run sync Redis call in thread to avoid blocking event loop)
        if task_id:
            import asyncio
            from app.worker.celery_app import celery_app
            await asyncio.to_thread(
                celery_app.control.revoke, task_id, terminate=True, signal="SIGTERM"
            )

        # 2. Mark job as cancelled in the DB (sync write for WAL safety)
        sync_repository.update_job_status(job_id, "error", stage="cancelled", error="Cancelled by user")

        # 3. Clean up storage files (best-effort)
        from app.core.storage import cleanup_job
        try:
            cleanup_job(job_id)
        except Exception:
            pass  # Best-effort — files may already be gone

        # 4. Invalidate cache
        await invalidate_job_cache(job_id)

        return {"status": "error", "task_id": task_id or ""}
