import logging

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.card_processing_service import CardProcessingService
from app.services.structure_adapter import StructureAdapter
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def process_card(self: Task, job_id: int, card_path: str) -> None:
    """Processes a single card: delegates to CardProcessingService, records progress in DB, and schedules retries."""
    logger.info("Starting process_card task for job %d, card: %s", job_id, card_path)

    settings = get_settings()
    service = CardProcessingService(job_id)
    try:
        with StructureAdapter(settings.ml_service_url) as ml_client:
            service._ml_client = ml_client
            service.process_card(card_path)

        # Record successful progress
        res = sync_repository.increment_progress(job_id, card_path, success=True)
        if res.is_complete:
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)

    except Exception as exc:
        logger.error(
            "Failed to process card %s for job %d: %s", card_path, job_id, exc,
            exc_info=True,
        )

        # If we have retries remaining, delegate retry to Celery
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        else:
            # Retries exhausted: mark as failed in DB and save error trace
            service.write_error_file(card_path, exc)
            res = sync_repository.increment_progress(
                job_id, card_path, success=False, error_message=str(exc)
            )
            if res.is_complete:
                from app.worker.tasks.aggregate import aggregate

                aggregate.delay(job_id)
