"""Celery task: aggregate — final BOM vs Cards comparison.

Triggered by ``process_card`` when ``processed + failed == total``
(atomic counter check in SQLite).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.bom_parser_service import (
    CardParseResult,
    CardPart,
    CardsData,
    CardSheetInfo,
    parse_bom,
)
from app.services.comparator_service import compare_all_configs
from app.services.normalizer import normalize_part_number
from app.services.report_service import generate_discrepancy_report
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

        logger.info("Parsing BOM from %s via legacy parser", bom_path)
        bom = parse_bom(bom_path)
        logger.info(
            "BOM parsed: %d parts, %d configs", len(bom.parts), len(bom.config_names)
        )

        # 2. Load cards data from JSON files
        job_dir = os.path.join(settings.storage_path, str(job_id))
        cards_dir = os.path.join(job_dir, "card_materials")

        card_results = []
        all_parts: dict[str, float] = {}
        original_part_numbers: dict[str, str] = {}
        part_names_ru: dict[str, str] = {}

        if os.path.exists(cards_dir):
            for fpath in sorted(Path(cards_dir).glob("**/*.json")):
                if fpath.name.endswith("_error.json"):
                    continue
                try:
                    with open(fpath, encoding="utf-8") as f:
                        parts_list = json.load(f)
                except Exception as exc:
                    logger.warning("Failed to load JSON file %s: %s", fpath, exc)
                    continue

                if not parts_list:
                    continue

                card_no = parts_list[0].get("card_no") if parts_list else fpath.stem

                card_parts = []
                aggregated_parts: dict[str, float] = {}
                for p in parts_list:
                    part_no = p["part_no"]
                    qty = p["qty"]
                    name_ru = p.get("name_ru", "")
                    card_parts.append(
                        CardPart(
                            part_number=part_no,
                            quantity=qty,
                            source_card=card_no,
                            source_sheet=p["sheet_name"],
                            name_ru=name_ru,
                        )
                    )
                    norm_pn = normalize_part_number(part_no)
                    aggregated_parts[norm_pn] = aggregated_parts.get(norm_pn, 0.0) + qty
                    all_parts[norm_pn] = all_parts.get(norm_pn, 0.0) + qty
                    if norm_pn not in original_part_numbers:
                        original_part_numbers[norm_pn] = part_no
                    if name_ru:
                        part_names_ru[norm_pn] = name_ru

                card_results.append(
                    CardParseResult(
                        card_number=card_no,
                        file_path=str(fpath),
                        sheets=[
                            CardSheetInfo(
                                card_number=card_no,
                                sheet_name=p["sheet_name"],
                                is_valid=True,
                                has_data=True,
                            )
                            for p in parts_list
                        ],
                        parts=card_parts,
                        aggregated_parts=aggregated_parts,
                    )
                )

        cards_data = CardsData(
            all_parts=all_parts,
            original_part_numbers=original_part_numbers,
            part_names_ru=part_names_ru,
            card_results=card_results,
            total_cards_processed=len(card_results),
            total_sheets_processed=sum(len(cr.sheets) for cr in card_results),
        )

        # 3. Load failed/corrupted cards from DB and append to cards_data
        failed_cards = _get_failed_cards(job_id)
        cards_data.corrupted_files = [fc["card_path"] for fc in failed_cards]
        cards_data.corrupted_files_detailed = [
            {"file": fc["card_path"], "error": fc["error_message"]}
            for fc in failed_cards
        ]

        logger.info(
            "Cards data constructed: %d processed, %d failed",
            len(card_results),
            len(failed_cards),
        )

        # 4. Compare all configs
        logger.info("Comparing BOM vs Cards via legacy matching engine")
        result = compare_all_configs(bom, cards_data, use_fuzzy=True)

        # 5. Generate Excel and Text report
        diff_path = os.path.join(job_dir, "diff.xlsx")
        logger.info("Generating spreadsheet report at %s", diff_path)
        generate_discrepancy_report(result, diff_path, bom=bom, cards_data=cards_data)

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
