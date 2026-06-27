import logging
import os
import zipfile

from celery import Task  # type: ignore[import-untyped]

from app.db import sync_repository
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def unpack(self: Task, job_id: int) -> None:
    """Reads ZIP contents streaming-only, creates cards records, and triggers analysis."""
    logger.info(f"Starting unpack task for job {job_id}")
    try:
        # 1. Update job stage to unpacking
        sync_repository.update_job_status(job_id, "processing", "unpacking")

        # 2. Get file paths
        _, archive_path = sync_repository.get_job_files(job_id)
        if not archive_path or not os.path.exists(archive_path):
            raise FileNotFoundError(f"Archive file not found: {archive_path}")

        # 3. Read ZIP table of contents (streaming metadata only, no extraction to disk)
        card_paths = []
        with zipfile.ZipFile(archive_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                filename = info.filename
                # Exclude hidden files or OS artifacts (like macOS metadata)
                if (
                    filename.startswith(".")
                    or "__MACOSX" in filename
                    or filename.split("/")[-1].startswith(".")
                ):
                    continue
                # We only process xlsx cards
                if filename.endswith(".xlsx"):
                    card_paths.append(filename)

        logger.info(
            f"Job {job_id}: found {len(card_paths)} operational card files in archive"
        )

        # 4. Save cards to DB and set total count
        sync_repository.create_cards(job_id, card_paths)

        # 5. Transition to next stage
        sync_repository.update_job_status(job_id, "processing", "analyzing_mapping")

        # 6. Trigger analyze task
        from app.worker.tasks.analyze_mapping import analyze_mapping

        analyze_mapping.delay(job_id)

    except Exception as exc:
        logger.error(f"Unpack failed for job {job_id}: {exc}", exc_info=True)
        # Attempt to mark the job as failed if error is unrecoverable or on final retry
        if self.request.retries >= self.max_retries:
            sync_repository.update_job_status(job_id, "error")
        raise self.retry(exc=exc)
