import logging

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.card_processing_service import CardProcessingService
from app.services.structure_adapter import StructureAdapter
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Cached StructureAdapter instance for the worker process
_ml_client: StructureAdapter | None = None


def get_ml_client() -> StructureAdapter:
    """Get or create the cached StructureAdapter instance for this process."""
    global _ml_client
    if _ml_client is None:
        settings = get_settings()
        _ml_client = StructureAdapter(settings.ml_service_url)
    return _ml_client


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def process_card(self: Task, job_id: int, card_path: str) -> None:
    """Processes a single card: delegates to CardProcessingService, records progress in DB, and schedules retries."""
    logger.info(f"Starting process_card task for job {job_id}, card: {card_path}")

    ml_client = get_ml_client()
    service = CardProcessingService(job_id, ml_client=ml_client)
    try:
        service.process_card(card_path)

        # Record successful progress
        res = sync_repository.increment_progress(job_id, card_path, success=True)
        if res.is_complete:
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)

    except Exception as exc:
        logger.error(
            f"Failed to process card {card_path} for job {job_id}: {exc}", exc_info=True
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
