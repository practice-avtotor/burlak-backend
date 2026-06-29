"""Celery task: package — archive translated cards and finalize job.

Triggered by ``aggregate`` after ``diff.xlsx`` is generated.

Workflow:
  1. Check if ``translated_cards/`` directory exists.
  2. Create ``translated_cards.zip`` from all XLSX files in that directory.
  3. Recursively delete ``translated_cards/`` directory.
  4. Count failed cards in DB.
  5. Set final job status: ``done`` (if failed == 0) or ``error`` (if failed > 0).
"""

from __future__ import annotations

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
    logger.info("Starting package task for job %d", job_id)
    try:
        job_dir = os.path.join(settings.storage_path, str(job_id))
        translated_cards_dir = os.path.join(job_dir, "translated_cards")
        output_zip_path = os.path.join(job_dir, "translated_cards.zip")

        # 1. Create ZIP archive of all translated xlsx cards
        _create_zip(translated_cards_dir, output_zip_path)

        # 2. Clean up temporary translated_cards directory
        _cleanup_translated_dir(translated_cards_dir)

        # 3. Count failed cards
        failed_count = _count_failed_cards(job_id)

        # 4. Set final job status
        if failed_count == 0:
            sync_repository.update_job_status(job_id, "done", "completed")
            logger.info("Job %d completed successfully (done)", job_id)
        else:
            sync_repository.update_job_status(job_id, "error", "completed_with_errors")
            logger.warning(
                "Job %d completed with %d failed cards (error)",
                job_id,
                failed_count,
            )

    except Exception as exc:
        logger.error("Packaging failed for job %d: %s", job_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            sync_repository.update_job_status(job_id, "error", "packaging_failed")
        raise self.retry(exc=exc)


def _create_zip(translated_cards_dir: str, output_zip_path: str) -> None:
    """Create ``translated_cards.zip`` from all XLSX files in *translated_cards_dir*."""
    if not os.path.isdir(translated_cards_dir):
        logger.warning(
            "translated_cards directory not found at %s — creating empty archive",
            translated_cards_dir,
        )
        os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)
        with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            pass
        logger.info("Empty archive created at %s", output_zip_path)
        return

    os.makedirs(os.path.dirname(output_zip_path), exist_ok=True)
    file_count = 0
    with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(translated_cards_dir):
            for fname in sorted(files):
                # Skip temporary Excel files
                if fname.startswith("~$"):
                    continue
                # Only include .xlsx files
                if not fname.lower().endswith((".xlsx", ".xls")):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, translated_cards_dir)
                try:
                    zf.write(full_path, rel_path)
                    file_count += 1
                except (FileNotFoundError, PermissionError) as exc:
                    logger.warning("Skipping unreadable file %s: %s", fname, exc)

    size_kb = (
        os.path.getsize(output_zip_path) / 1024
        if os.path.exists(output_zip_path)
        else 0
    )
    logger.info(
        "Archive created at %s (%d files, %.1f KB)",
        output_zip_path,
        file_count,
        size_kb,
    )


def _cleanup_translated_dir(translated_cards_dir: str) -> None:
    """Recursively delete the *translated_cards_dir* after successful archiving."""
    if not os.path.isdir(translated_cards_dir):
        logger.info(
            "translated_cards directory already removed or never created: %s",
            translated_cards_dir,
        )
        return
    try:
        shutil.rmtree(translated_cards_dir, ignore_errors=True)
        if os.path.exists(translated_cards_dir):
            logger.warning(
                "Failed to fully remove %s — some files may remain",
                translated_cards_dir,
            )
        else:
            logger.info("Removed temporary directory %s", translated_cards_dir)
    except Exception as exc:
        logger.error("Error removing %s: %s", translated_cards_dir, exc)


def _count_failed_cards(job_id: int) -> int:
    """Count cards with status 'failed' for the given *job_id*."""
    return len(sync_repository.get_failed_cards(job_id))
