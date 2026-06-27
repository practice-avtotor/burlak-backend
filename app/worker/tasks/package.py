"""Package task: build translated_cards.zip, finalize job status."""

from __future__ import annotations

import logging
from typing import Any

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=30,
    name="app.worker.tasks.package.package",
)
def package(self: Any, job_id: int) -> None:
    """Package translated cards into ZIP archive.

    Pure orchestrator — delegates ZIP creation to archive_service.
    Updates final job status to 'done' or 'error' (if any cards failed).
    """
    from app.db import sync_repository
    from app.services.archive_service import create_translated_zip

    try:
        create_translated_zip(job_id)

        job = sync_repository.get_job(job_id)
        if job is not None and job["failed"] > 0:
            sync_repository.set_job_status(job_id, "error")
        else:
            sync_repository.set_job_status(job_id, "done")

        logger.info("Package completed for job %d", job_id)

    except FileNotFoundError as exc:
        logger.warning("No translated cards for job %d: %s", job_id, exc)
        sync_repository.set_job_status(job_id, "done")
    except Exception as exc:
        logger.error("Package failed for job %d: %s", job_id, exc)
        sync_repository.set_job_error(job_id, f"Packaging failed: {exc}")
