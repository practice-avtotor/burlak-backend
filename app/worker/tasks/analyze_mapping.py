"""Analyze mapping task: send BOM + sample cards to ML service."""

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
    name="app.worker.tasks.analyze_mapping.analyze_mapping",
)
def analyze_mapping(self: Any, job_id: int) -> None:
    """Analyze BOM and card structure via ML service.

    After saving mapping_config, dispatches process_card.delay()
    for every card in the job — this is the fan-out point.
    """
    from app.db import sync_repository

    try:
        job = sync_repository.get_job(job_id)
        if job is None:
            logger.error("Job %d not found", job_id)
            return

        # TODO: Implement when excel_service and translation_adapter are ready
        # 1. Read BOM -> JSON snapshot (first 20 rows)
        # 2. Read sample cards -> JSON snapshot
        # 3. POST to ML service -> receive mapping_config
        # 4. Save mapping_config to SQLite

        mapping_config = {"status": "pending_ml_integration"}
        sync_repository.update_mapping_config(job_id, mapping_config)
        sync_repository.update_job_stage(job_id, "processing_cards")

        # Fan-out: dispatch process_card for every card
        card_paths = sync_repository.get_card_paths(job_id)
        for card_path in card_paths:
            from app.worker.tasks.process_card import process_card

            process_card.delay(job_id, card_path)

        logger.info(
            "Analyze mapping completed for job %d, dispatched %d card tasks",
            job_id,
            len(card_paths),
        )

    except Exception as exc:
        logger.error("Analyze mapping failed for job %d: %s", job_id, exc)
        raise self.retry(exc=exc)
