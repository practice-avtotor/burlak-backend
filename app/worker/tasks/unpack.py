"""Unpack task: stream ZIP table of contents, create card DB records.

Reads the archive TOC via zipfile.infolist() (never extractall),
creates card records in SQLite, then triggers analyze_mapping.
"""

from __future__ import annotations

import logging
import zipfile
from typing import Any

from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(  # type: ignore[untyped-decorator]
    bind=True,
    max_retries=3,
    retry_backoff=True,
    retry_backoff_max=30,
    name="app.worker.tasks.unpack.unpack",
)
def unpack(self: Any, job_id: int) -> None:
    """Stream ZIP TOC and create card records."""
    from app.core.config import get_settings
    from app.db import sync_repository

    settings = get_settings()
    archive_path = f"{settings.storage_path}/{job_id}/archive.zip"

    try:
        with zipfile.ZipFile(archive_path) as zf:
            card_paths = [
                info.filename
                for info in zf.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith((".xlsx", ".xls"))
            ]

        if not card_paths:
            logger.error("No Excel files found in archive for job %d", job_id)
            sync_repository.set_job_error(job_id, "No Excel files found in archive")
            return

        sync_repository.create_cards_bulk(job_id, card_paths)
        sync_repository.update_job_stage(job_id, "analyzing_mapping")

        logger.info("Unpacked %d cards for job %d", len(card_paths), job_id)

        from app.worker.tasks.analyze_mapping import analyze_mapping

        analyze_mapping.delay(job_id)

    except zipfile.BadZipFile as exc:
        logger.error("Bad ZIP file for job %d: %s", job_id, exc)
        sync_repository.set_job_error(job_id, f"Invalid ZIP archive: {exc}")
    except Exception as exc:
        logger.error("Unpack failed for job %d: %s", job_id, exc)
        raise self.retry(exc=exc)
