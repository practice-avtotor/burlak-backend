"""Process card task: parse, translate, write results."""

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
    name="app.worker.tasks.process_card.process_card",
)
def process_card(self: Any, job_id: int, card_path: str) -> None:
    """Process a single operational card.

    Retries transient errors up to max_retries. Only marks the card
    as failed after all retries are exhausted — partial results are
    more dangerous than no results in a manufacturing context.
    """
    from app.db import sync_repository

    try:
        # TODO: Implement when services are ready
        # 1. archive_service.read_card(job_id, card_path) -> bytes
        # 2. excel_service.parse(card_data, mapping_config) -> parsed data
        # 3. translation_adapter.translate(parsed) -> translated data
        # 4. excel_service.write_translated(job_id, card_path, translated)
        # 5. Write card_XX_materials.json

        result = sync_repository.increment_progress(job_id, card_path, success=True)

        logger.info(
            "Card processed: job=%d path=%s progress=%d/%d",
            job_id,
            card_path,
            result.processed,
            result.total,
        )

        if result.is_complete:
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)

    except Exception as exc:
        # Only retry if retries remain — do NOT mark as failed yet
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)

        # Retries exhausted — mark card as failed
        logger.error(
            "Process card failed (retries exhausted): job=%d path=%s error=%s",
            job_id,
            card_path,
            exc,
        )
        result = sync_repository.increment_progress(
            job_id, card_path, success=False, error_message=str(exc)
        )

        if result.is_complete:
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)
