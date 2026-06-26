"""Archive service: ZIP packaging and streaming operations.

Delegates all file-level archive work so that Celery tasks
remain pure orchestrators with no inline business logic.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def create_translated_zip(job_id: int) -> Path:
    """Archive all translated card XLSX files into a single ZIP.

    Args:
        job_id: The job ID.

    Returns:
        Path to the created translated_cards.zip.

    Raises:
        FileNotFoundError: No translated files found for the job.
    """
    settings = get_settings()
    cards_dir = Path(settings.storage_path) / str(job_id) / "cards"
    output_path = Path(settings.storage_path) / str(job_id) / "translated_cards.zip"

    translated_files = list(cards_dir.glob("*_translated.xlsx"))

    if not translated_files:
        raise FileNotFoundError(
            f"No translated cards found for job {job_id} in {cards_dir}"
        )

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in translated_files:
            zf.write(file_path, file_path.name)

    logger.info(
        "Packaged %d translated cards for job %d -> %s",
        len(translated_files),
        job_id,
        output_path,
    )
    return output_path
