"""Celery task: aggregate — final BOM vs Cards comparison.

Triggered by ``process_card`` when ``processed + failed == total``
(atomic counter check in SQLite).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.schemas.cards import CardParseResult, CardPart, CardsData, CardSheetInfo
from app.services.bom_parser_service import parse_bom
from app.services.comparator_service import compare_all_configs, verify_integrity
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

        mapping_config = sync_repository.get_mapping_config(job_id)
        if not mapping_config:
            raise ValueError(f"Mapping configuration not found for job {job_id}")

        try:
            bom_sheets_cfg = mapping_config["bom"]["sheets"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"BOM structure configuration is missing or invalid for job {job_id}"
            ) from exc

        if not bom_sheets_cfg:
            raise ValueError(f"BOM sheets configuration is empty for job {job_id}")

        logger.info("Parsing BOM from %s", bom_path)
        bom = parse_bom(bom_path, sheets_config=bom_sheets_cfg)
        logger.info(
            "BOM parsed: %d parts, %d configs", len(bom.parts), len(bom.config_names)
        )

        # If config-based parsing found 0 parts, try auto-detection
        if len(bom.parts) == 0:
            logger.warning(
                "Config-based BOM parsing found 0 parts for job %d, "
                "trying auto-detection", job_id,
            )
            from app.services.bom_parser_service import auto_detect_bom_sheets
            auto_sheets = auto_detect_bom_sheets(bom_path)
            if auto_sheets:
                logger.info(
                    "Auto-detected %d BOM sheets, retrying parse", len(auto_sheets)
                )
                bom = parse_bom(bom_path, sheets_config=auto_sheets)
                logger.info(
                    "BOM re-parsed: %d parts, %d configs",
                    len(bom.parts), len(bom.config_names),
                )

        # 2. Load cards data from JSON files
        job_dir = os.path.join(settings.storage_path, str(job_id))
        cards_dir = os.path.join(job_dir, "card_materials")

        card_results = []
        all_parts: dict[str, float] = {}
        original_part_numbers: dict[str, str] = {}
        part_names_ru: dict[str, str] = {}
        part_sources: dict[str, list[tuple[str, str, float]]] = {}

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
                    # Track which cards contain each part (for report)
                    if norm_pn not in part_sources:
                        part_sources[norm_pn] = []
                    part_sources[norm_pn].append((card_no, p["sheet_name"], qty))

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
            part_sources=part_sources,
            card_results=card_results,
            total_cards_processed=len(card_results),
            total_sheets_processed=sum(len(cr.sheets) for cr in card_results),
        )

        # 3. Load failed/corrupted cards from DB and append to cards_data
        failed_cards = sync_repository.get_failed_cards(job_id)
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

        # 3b. Check if we got any useful data at all
        if len(bom.parts) == 0 and len(card_results) == 0:
            error_msg = (
                "Fallback mapping не подходит к загруженным файлам. "
                "BOM: 0 деталей, Карты: 0 обработано. "
                "См. JSON-запросы в /data/requests/{job_id}/ — "
                "подготовьте mapping_config.json вручную."
            )
            logger.error("Job %d: %s", job_id, error_msg)
            sync_repository.update_job_status(
                job_id, "error", stage="aggregating", error=error_msg
            )
            return

        # 4. Compare all configs
        logger.info("Comparing BOM vs Cards via legacy matching engine")
        result = compare_all_configs(bom, cards_data, use_fuzzy=True)

        # 4b. Verify integrity of matching results
        integrity = verify_integrity(result)
        if not integrity.is_ok:
            logger.warning(
                "BOM discrepancy integrity check failed for job %d: config_issues=%s, global_issue=%s",
                job_id,
                integrity.config_issues,
                integrity.global_issue,
            )
        else:
            logger.info(
                "BOM discrepancy integrity check passed successfully for job %d", job_id
            )

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
            sync_repository.update_job_status(
                job_id, "error", stage="aggregating_failed", error=str(exc)
            )
            return
        raise self.retry(exc=exc)
