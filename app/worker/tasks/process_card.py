import io
import json
import logging
import os
import traceback
import zipfile

import openpyxl  # type: ignore[import-untyped]
from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.card_parser_service import (
    classify_file,
    extract_card_number,
    find_sheet_mapping,
    find_table_end,
)
from app.services.structure_adapter import StructureAdapter
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def process_card(self: Task, job_id: int, card_path: str) -> None:
    """Processes a single card: classifies, parses, translates via ML, writes output, and updates progress."""
    logger.info(f"Starting process_card task for job {job_id}, card: {card_path}")

    # Define directories
    storage_root = settings.storage_path
    job_dir = os.path.join(storage_root, str(job_id))
    translated_cards_dir = os.path.join(job_dir, "translated_cards")
    card_materials_dir = os.path.join(job_dir, "card_materials")

    try:
        # 1. Retrieve job details & files
        _, archive_path = sync_repository.get_job_files(job_id)
        if not archive_path or not os.path.exists(archive_path):
            raise FileNotFoundError(f"Archive file not found: {archive_path}")

        mapping_config = sync_repository.get_mapping_config(job_id)
        if not mapping_config:
            raise ValueError(f"Mapping configuration not found for job {job_id}")

        # 2. Open archive and stream card data
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                card_bytes = zf.read(card_path)
            except KeyError:
                raise FileNotFoundError(f"Card {card_path} not found in zip archive")

        # 3. Classify card
        classification, format_group = classify_file(card_path, mapping_config["cards"])
        logger.info(
            f"Card {card_path} classified as '{classification}' (format group: '{format_group}')"
        )

        # Destination paths
        dest_xlsx_path = os.path.join(translated_cards_dir, card_path)
        os.makedirs(os.path.dirname(dest_xlsx_path), exist_ok=True)

        card_materials_path = os.path.join(card_materials_dir, f"{card_path}.json")
        os.makedirs(os.path.dirname(card_materials_path), exist_ok=True)

        if classification != "operational_card":
            # Non-operational card (service or unknown). Save original directly and skip translation/parsing.
            with open(dest_xlsx_path, "wb") as f:
                f.write(card_bytes)

            # Create an empty materials file so aggregate task doesn't fail
            with open(card_materials_path, "w", encoding="utf-8") as f:
                json.dump([], f)

            # Increment progress and trigger next step if finished
            res = sync_repository.increment_progress(job_id, card_path, success=True)
            if res.is_complete:
                from app.worker.tasks.aggregate import aggregate

                aggregate.delay(job_id)
            return

        # 4. Parse the operational card
        format_config = mapping_config["cards"]["formats"][format_group]
        card_no = extract_card_number(card_path, format_config)

        # Load workbook (data_only=False to preserve styles, formulas, and fonts when saving)
        wb = openpyxl.load_workbook(io.BytesIO(card_bytes), data_only=False)

        parts = []
        unique_chinese_texts = set()

        # We need mapping cell reference (sheet, row, col) to keep track of where to write translated values
        cell_updates = []

        for sheet_name in wb.sheetnames:
            sheet_mapping = find_sheet_mapping(sheet_name, format_config["sheets"])
            if not sheet_mapping:
                continue

            ws = wb[sheet_name]
            cols = sheet_mapping["columns"]
            part_no_col = cols["part_no"]["col_index"]
            name_cn_col = cols["name_cn"]["col_index"]
            qty_col = cols["qty"]["col_index"]

            data_start = sheet_mapping["data_start_row"]
            end_row = find_table_end(
                ws, data_start, boundaries_config=sheet_mapping["table_boundaries"]
            )

            for r in range(data_start, end_row + 1):
                part_no_val = ws.cell(row=r, column=part_no_col).value
                name_cn_val = ws.cell(row=r, column=name_cn_col).value
                qty_val = ws.cell(row=r, column=qty_col).value

                # Skip empty parts
                if not part_no_val:
                    continue

                part_no = str(part_no_val).strip()
                name_cn = str(name_cn_val).strip() if name_cn_val is not None else ""

                # Standardize quantity
                qty = 0
                if qty_val is not None:
                    try:
                        qty = int(float(str(qty_val).strip()))
                    except ValueError:
                        pass

                parts.append(
                    {
                        "card_no": card_no,
                        "sheet_name": sheet_name,
                        "row_index": r,
                        "part_no": part_no,
                        "name_cn": name_cn,
                        "name_ru": name_cn,  # default to CN
                        "qty": qty,
                    }
                )

                if name_cn:
                    unique_chinese_texts.add(name_cn)
                    cell_updates.append(
                        {
                            "sheet_name": sheet_name,
                            "row": r,
                            "col": name_cn_col,
                            "original_val": name_cn,
                        }
                    )

        # 5. Translate Chinese names
        translations = {}
        if unique_chinese_texts:
            ml_client = StructureAdapter(settings.ml_service_url)
            try:
                translations = ml_client.translate_batch(list(unique_chinese_texts))
            except Exception as e:
                logger.warning(
                    f"Translation batch failed for {card_path}: {e}. Falling back to original texts."
                )

        # 6. Apply style-preserving updates and update parts
        for part in parts:
            part["name_ru"] = translations.get(part["name_cn"], part["name_cn"])

        for update in cell_updates:
            ws = wb[update["sheet_name"]]
            r = update["row"]
            col = update["col"]
            original = update["original_val"]
            ws.cell(row=r, column=col).value = translations.get(original, original)

        # 7. Write results to shared storage
        # Save translated workbook
        wb.save(dest_xlsx_path)

        # Save parts list as JSON
        with open(card_materials_path, "w", encoding="utf-8") as f:
            json.dump(parts, f, ensure_ascii=False, indent=2)

        # 8. Increment progress in DB
        res = sync_repository.increment_progress(job_id, card_path, success=True)
        if res.is_complete:
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)

    except Exception as exc:
        logger.error(
            f"Failed to process card {card_path} for job {job_id}: {exc}", exc_info=True
        )

        # Write error file for debugging and inclusion in final diff report
        try:
            error_materials_path = os.path.join(
                card_materials_dir, f"{card_path}_error.json"
            )
            os.makedirs(os.path.dirname(error_materials_path), exist_ok=True)
            with open(error_materials_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "card_path": card_path,
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as write_err:
            logger.error(f"Failed to write error marker for {card_path}: {write_err}")

        # Task itself succeeds to not block the final pipeline aggregator
        # But we record the failure in DB
        res = sync_repository.increment_progress(
            job_id, card_path, success=False, error_message=str(exc)
        )
        if res.is_complete:
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)

        # Re-raise celery task retry unless we ran out of retries
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
