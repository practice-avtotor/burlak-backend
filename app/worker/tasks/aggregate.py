"""Celery task: aggregate — final BOM vs Cards comparison.

Triggered by ``process_card`` when ``processed + failed == total``
(atomic counter check in SQLite).

Workflow:
  1. Load BOM.xlsx from shared storage.
  2. Aggregate all card_*_materials.json from job directory.
  3. Compare via ComparisonService (Pandas merge/join).
  4. Fetch failed cards from DB.
  5. Generate diff.xlsx report.
  6. Update job stage to ``packaging``.
  7. Trigger ``package.delay(job_id)``.
"""

from __future__ import annotations

import logging
import os
import sqlite3

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.comparison_service import ComparisonService
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def aggregate(self: Task, job_id: int) -> None:
    """Aggregates card materials, compares them with BOM, and generates the difference report."""
    logger.info("Starting aggregate task for job %d", job_id)
    try:
        sync_repository.update_job_status(job_id, "processing", "aggregating")

        # 1. Load BOM
        bom_path, _archive_path = sync_repository.get_job_files(job_id)
        if not bom_path:
            raise ValueError(f"Job {job_id} has no bom_path set")
        mapping_config = sync_repository.get_mapping_config(job_id)
        logger.info("Loading BOM from %s", bom_path)
        bom_df = ComparisonService.load_bom(bom_path, mapping_config)
        logger.info("BOM loaded: %d rows", len(bom_df))

        # 2. Load cards data
        job_dir = os.path.join(settings.storage_path, str(job_id))
        cards_df = ComparisonService.load_cards_data(job_dir)
        logger.info("Cards data loaded: %d rows", len(cards_df))

        # 3. Compare
        result = ComparisonService.compare(bom_df, cards_df)
        summary = result["summary"]
        logger.info(
            "Comparison complete: %d discrepancies "
            "(only_in_bom=%d, only_in_cards=%d, qty_mismatch=%d)",
            summary["total_discrepancies"],
            summary["only_in_bom"],
            summary["only_in_cards"],
            summary["qty_mismatch"],
        )

        # 4. Fetch failed cards
        failed_cards = _get_failed_cards(job_id)
        if failed_cards:
            logger.warning("Job %d has %d failed cards", job_id, len(failed_cards))

        # 5. Generate report
        diff_path = os.path.join(job_dir, "diff.xlsx")
        ComparisonService.generate_report(result, diff_path, failed_cards)
        logger.info("Report saved to %s", diff_path)

        # 6. Transition stage to packaging
        sync_repository.update_job_status(job_id, "processing", "packaging")

        # 7. Trigger package task
        from app.worker.tasks.package import package as package_task

        package_task.delay(job_id)

    except Exception as exc:
        logger.error("Aggregation failed for job %d: %s", job_id, exc, exc_info=True)
        if self.request.retries >= self.max_retries:
            sync_repository.update_job_status(job_id, "error", "aggregating_failed")
        raise self.retry(exc=exc)


def _get_failed_cards(job_id: int) -> list[dict[str, str]]:
    """Fetch failed cards from DB using a direct SQLite query."""
    db_path = settings.sqlite_db_path
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT card_path, error_message FROM cards WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        rows = cursor.fetchall()
        return [
            {"card_path": r["card_path"], "error_message": r["error_message"] or ""}
            for r in rows
        ]
    finally:
        conn.close()
