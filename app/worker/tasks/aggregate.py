"""Aggregate task: compare BOM vs cards, generate diff report."""

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
    name="app.worker.tasks.aggregate.aggregate",
)
def aggregate(self: Any, job_id: int) -> None:
    """Aggregate results and generate diff report.

    Stage transitions: processing_cards -> aggregating -> packaging.
    """
    from app.db import sync_repository

    try:
        job = sync_repository.get_job(job_id)
        if job is None:
            logger.error("Job %d not found during aggregation", job_id)
            return

        sync_repository.update_job_stage(job_id, "aggregating")

        # TODO: Implement when comparison_service is ready
        # 1. Load BOM.xlsx -> pandas DataFrame
        # 2. Aggregate all card_XX_materials.json
        # 3. Compare BOM vs aggregated cards
        # 4. Generate diff.xlsx (with failed cards listed)
        # 5. Save to shared storage

        sync_repository.update_job_stage(job_id, "packaging")

        logger.info("Aggregation completed for job %d", job_id)

        from app.worker.tasks.package import package

        package.delay(job_id)

    except Exception as exc:
        logger.error("Aggregate failed for job %d: %s", job_id, exc)
        raise self.retry(exc=exc)
