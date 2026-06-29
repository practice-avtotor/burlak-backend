import io
import json
import logging
import os
import re
import traceback
import zipfile

import openpyxl
from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.card_parser_service import CardParserService
from app.services.structure_adapter import StructureAdapter
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


def extract_card_number(card_path: str) -> str:
    """Extract card number from card path/filename.

    Fallback to basename without extension.
    """
    basename = os.path.basename(card_path)
    name_no_ext, _ = os.path.splitext(basename)

    # Try typical prefix-AS-number pattern
    match = re.match(r"^([a-zA-Z0-9]+-AS-[0-9]+)", name_no_ext, re.IGNORECASE)
    if match:
        return match.group(1)

    match = re.match(r"^([a-zA-Z0-9]+-[0-9]+)", name_no_ext)
    if match:
        return match.group(1)

    return name_no_ext


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
        parser = CardParserService(mapping_config)
        filename = os.path.basename(card_path)
        classification = parser.classify(filename)
        logger.info(f"Card {card_path} classified as '{classification}'")

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
        parse_result = parser.parse_card(card_bytes, filename)
        if parse_result.error:
            raise ValueError(parse_result.error)

        card_no = extract_card_number(card_path)

        # Extract unique Chinese names for translation
        unique_chinese_texts = {p.name for p in parse_result.parts if p.name}

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
        parts = []
        for p in parse_result.parts:
            parts.append(
                {
                    "card_no": card_no,
                    "sheet_name": p.source_sheet,
                    "row_index": p.row,
                    "part_no": p.part_number,
                    "name_cn": p.name,
                    "name_ru": translations.get(p.name, p.name),
                    "qty": p.quantity,
                }
            )

        # Save translated workbook (data_only=False to preserve styles, formulas, and fonts when saving)
        name_col = mapping_config.get("cards", {}).get("columns", {}).get("name", 0)
        wb = openpyxl.load_workbook(io.BytesIO(card_bytes), data_only=False)
        try:
            for p in parse_result.parts:
                if p.name and name_col > 0:
                    ws = wb[p.source_sheet]
                    ws.cell(row=p.row, column=name_col).value = translations.get(
                        p.name, p.name
                    )
            wb.save(dest_xlsx_path)
        finally:
            wb.close()

        # 7. Write results to shared storage
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
