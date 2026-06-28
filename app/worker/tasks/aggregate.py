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

from app.core.storage import get_job_dir
from app.db.sync_repository import (
    get_job_files,
    get_mapping_config,
    update_job_status,
)
from app.services.comparison_service import ComparisonService

logger = logging.getLogger(__name__)


def aggregate(job_id: int) -> None:
    """Run the aggregation pipeline for *job_id*.

    This function is designed to be called both from Celery tasks
    (via ``aggregate.delay(job_id)``) and directly in tests.
    """
    logger.info("Starting aggregation for job %d", job_id)

    try:
        # 1. Load BOM
        bom_path, _ = get_job_files(job_id)
        mapping_config = get_mapping_config(job_id)
        logger.info("Loading BOM from %s", bom_path)
        bom_df = ComparisonService.load_bom(bom_path, mapping_config)
        logger.info("BOM loaded: %d rows", len(bom_df))

        # 2. Load cards data
        job_dir = get_job_dir(job_id)
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
        output_path = job_dir / "diff.xlsx"
        ComparisonService.generate_report(result, str(output_path), failed_cards)
        logger.info("Report saved to %s", output_path)

        # 6. Update status to packaging
        update_job_status(job_id, "processing", "packaging")

        # 7. Trigger package task
        _trigger_package(job_id)

        logger.info("Aggregation for job %d completed successfully", job_id)

    except Exception:
        logger.exception("Aggregation failed for job %d", job_id)
        try:
            update_job_status(job_id, "error", "aggregating_failed")
        except Exception:
            logger.exception("Failed to update job status after aggregation error")


def _get_failed_cards(job_id: int) -> list[dict[str, str]]:
    """Fetch failed cards from DB using a direct SQLite query.

    Uses the same connection pattern as ``sync_repository``.
    """
    import sqlite3

    from app.core.config import get_settings

    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT card_path, error_message FROM cards WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        rows = cursor.fetchall()
        return [{"card_path": r["card_path"], "error_message": r["error_message"] or ""} for r in rows]
    finally:
        conn.close()


def _trigger_package(job_id: int) -> None:
    """Trigger the package Celery task.

    Uses a lazy import to avoid circular dependencies at module level.
    """
    try:
        from app.worker.tasks.package import package

        package.delay(job_id)
    except ImportError:
        logger.warning(
            "package task not available yet — skipping trigger for job %d", job_id
        )
    except Exception as exc:
        logger.error("Failed to trigger package task for job %d: %s", job_id, exc)