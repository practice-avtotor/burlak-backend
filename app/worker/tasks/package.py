import logging
import os
import shutil
import zipfile

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def package(self: Task, job_id: int) -> None:
    """Packages all translated card documents into a single ZIP file and cleans up temporary files."""
    logger.info(f"Starting package task for job {job_id}")
    try:
        # Determine job status
        # We check sync_repository to see if there are any failures
        db_path = settings.db_url
        if db_path.startswith("sqlite:///"):
            db_path = db_path[len("sqlite:///") :]

        import sqlite3

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT processed, failed, total FROM jobs WHERE id = ?", (job_id,)
        )
        job = cursor.fetchone()
        conn.close()

        failed = 0
        if job:
            processed, failed, total = job
            logger.info(
                f"Job {job_id} stats: processed={processed}, failed={failed}, total={total}"
            )

        job_dir = os.path.join(settings.storage_path, str(job_id))
        translated_cards_dir = os.path.join(job_dir, "translated_cards")
        output_zip_path = os.path.join(job_dir, "translated_cards.zip")

        # Create zip archive of all translated xlsx cards (if the directory exists)
        if os.path.exists(translated_cards_dir):
            with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(translated_cards_dir):
                    for file in files:
                        full_path = os.path.join(root, file)
                        # Archive path should be relative to the translated_cards_dir
                        rel_path = os.path.relpath(full_path, translated_cards_dir)
                        zf.write(full_path, rel_path)
            logger.info(f"Created ZIP archive at {output_zip_path}")
        else:
            # Create an empty zip if no cards were processed/translated
            with zipfile.ZipFile(output_zip_path, "w") as zf:
                pass
            logger.warning(
                f"No translated cards folder found at {translated_cards_dir}. Created empty ZIP."
            )

        # Clean up temporary translated_cards directory
        if os.path.exists(translated_cards_dir):
            shutil.rmtree(translated_cards_dir)
            logger.info(f"Cleaned up temporary directory: {translated_cards_dir}")

        # Set final job status
        final_status = "error" if failed > 0 else "done"
        sync_repository.update_job_status(job_id, final_status, None)
        logger.info(
            f"Finished packaging task for job {job_id} with status: {final_status}"
        )

    except Exception as exc:
        logger.error(f"Packaging failed for job {job_id}: {exc}", exc_info=True)
        if self.request.retries >= self.max_retries:
            sync_repository.update_job_status(job_id, "error")
        raise self.retry(exc=exc)
