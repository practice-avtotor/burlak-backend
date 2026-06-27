import logging
import os

import openpyxl  # type: ignore[import-untyped]
from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def aggregate(self: Task, job_id: int) -> None:
    """Aggregates card materials, compares them with BOM, and generates the difference report."""
    logger.info(f"Starting aggregate task for job {job_id}")
    try:
        sync_repository.update_job_status(job_id, "processing", "aggregating")

        # Create a mock diff.xlsx file in the job's directory
        job_dir = os.path.join(settings.storage_path, str(job_id))
        os.makedirs(job_dir, exist_ok=True)
        diff_path = os.path.join(job_dir, "diff.xlsx")

        wb = openpyxl.Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Discrepancy Report"
        ws.append(
            [
                "Part Number",
                "Name CN",
                "Name RU",
                "BOM Qty",
                "Cards Qty",
                "Difference",
                "Status",
            ]
        )
        ws.append(["M6-BOLT", "螺栓M6", "Болт M6", 10, 10, 0, "Matched"])

        # Check if any errors occurred during processing
        card_materials_dir = os.path.join(job_dir, "card_materials")
        errors = []
        if os.path.exists(card_materials_dir):
            for filename in os.listdir(card_materials_dir):
                if filename.endswith("_error.json"):
                    errors.append(filename)

        if errors:
            ws.append([])
            ws.append(["Failed Cards Report"])
            ws.append(["Card Filename", "Error Message"])
            for err_file in errors:
                ws.append([err_file, "Parsing error occurred"])

        wb.save(diff_path)
        logger.info(f"Saved report to {diff_path}")

        # Transition stage to packaging
        sync_repository.update_job_status(job_id, "processing", "packaging")

        # Trigger package task
        from app.worker.tasks.package import package

        package.delay(job_id)

    except Exception as exc:
        logger.error(f"Aggregation failed for job {job_id}: {exc}", exc_info=True)
        if self.request.retries >= self.max_retries:
            sync_repository.update_job_status(job_id, "error")
        raise self.retry(exc=exc)
