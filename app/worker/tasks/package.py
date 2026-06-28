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
import shutil
import sqlite3
import zipfile
from pathlib import Path

from app.core.config import get_settings
from app.core.storage import get_job_dir
from app.db.sync_repository import update_job_status

logger = logging.getLogger(__name__)


def package(job_id: int) -> None:
    """Run the packaging pipeline for *job_id*.

    This function is designed to be called both from Celery tasks
    (via ``package.delay(job_id)``) and directly in tests.
    """
    logger.info("Starting packaging for job %d", job_id)

    try:
        job_dir = get_job_dir(job_id)
        translated_dir = job_dir / "translated_cards"
        zip_path = job_dir / "translated_cards.zip"

        # 1. Create ZIP archive
        _create_zip(translated_dir, zip_path)

        # 2. Cleanup: remove translated_cards/ directory
        _cleanup_translated_dir(translated_dir)

        # 3. Count failed cards
        failed_count = _count_failed_cards(job_id)

        # 4. Set final status
        if failed_count == 0:
            update_job_status(job_id, "done", "completed")
            logger.info("Job %d completed successfully (done)", job_id)
        else:
            update_job_status(job_id, "error", "completed_with_errors")
            logger.warning(
                "Job %d completed with %d failed cards (error)",
                job_id,
                failed_count,
            )

    except Exception:
        logger.exception("Packaging failed for job %d", job_id)
        try:
            update_job_status(job_id, "error", "packaging_failed")
        except Exception:
            logger.exception("Failed to update job status after packaging error")


def _create_zip(translated_dir: Path, zip_path: Path) -> None:
    """Create ``translated_cards.zip`` from all XLSX files in *translated_dir*."""
    if not translated_dir.is_dir():
        logger.warning(
            "translated_cards directory not found at %s — creating empty archive",
            translated_dir,
        )
        # Create empty ZIP
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            pass
        logger.info("Empty archive created at %s", zip_path)
        return

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in sorted(translated_dir.iterdir()):
            if not fpath.is_file():
                continue
            # Skip temporary Excel files
            if fpath.name.startswith("~$"):
                continue
            # Only include .xlsx files
            if not fpath.suffix.lower() in (".xlsx", ".xls"):
                continue
            try:
                zf.write(str(fpath), arcname=fpath.name)
                file_count += 1
            except (FileNotFoundError, PermissionError) as exc:
                logger.warning("Skipping unreadable file %s: %s", fpath.name, exc)

    logger.info(
        "Archive created at %s (%d files, %.1f KB)",
        zip_path,
        file_count,
        zip_path.stat().st_size / 1024 if zip_path.exists() else 0,
    )


def _cleanup_translated_dir(translated_dir: Path) -> None:
    """Recursively delete the *translated_dir* after successful archiving."""
    if not translated_dir.is_dir():
        logger.info("translated_cards directory already removed or never created")
        return
    try:
        shutil.rmtree(str(translated_dir), ignore_errors=True)
        if translated_dir.exists():
            logger.warning(
                "Failed to fully remove %s — some files may remain", translated_dir
            )
        else:
            logger.info("Removed temporary directory %s", translated_dir)
    except Exception as exc:
        logger.error("Error removing %s: %s", translated_dir, exc)


def _count_failed_cards(job_id: int) -> int:
    """Count cards with status 'failed' for the given *job_id*."""
    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) AS cnt FROM cards WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        row = cursor.fetchone()
        return row[0] if row else 0
    finally:
        conn.close()