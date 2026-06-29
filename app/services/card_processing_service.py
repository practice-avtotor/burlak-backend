from __future__ import annotations

import io
import json
import logging
import os
import re
import traceback
import zipfile

import openpyxl

from app.core.config import get_settings
from app.db import sync_repository
from app.services.card_parser_service import (
    _DEFAULT_SERVICE_KEYWORDS,
    CardParserService,
)
from app.services.splitter import CardSplitter
from app.services.structure_adapter import StructureAdapter
from app.services.xls_converter import convert_xls_to_xlsx

logger = logging.getLogger(__name__)


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


class CardProcessingService:
    """Service to process a single operational card including splitting, translation, and Excel output generation."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self.settings = get_settings()

    def process_card(self, card_path: str) -> None:
        logger.info(f"Processing card for job {self.job_id}: {card_path}")

        # Define directories
        storage_root = self.settings.storage_path
        job_dir = os.path.join(storage_root, str(self.job_id))
        translated_cards_dir = os.path.join(job_dir, "translated_cards")
        card_materials_dir = os.path.join(job_dir, "card_materials")

        # 1. Retrieve job details & files
        _, archive_path = sync_repository.get_job_files(self.job_id)
        if not archive_path or not os.path.exists(archive_path):
            raise FileNotFoundError(f"Archive file not found: {archive_path}")

        mapping_config = sync_repository.get_mapping_config(self.job_id)
        if not mapping_config:
            raise ValueError(f"Mapping configuration not found for job {self.job_id}")

        # 2. Open archive and stream card data
        with zipfile.ZipFile(archive_path, "r") as zf:
            try:
                card_bytes = zf.read(card_path)
            except KeyError:
                raise FileNotFoundError(f"Card {card_path} not found in zip archive")

        filename = os.path.basename(card_path)

        # 2b. XLS conversion to XLSX via LibreOffice if needed
        if card_path.lower().endswith(".xls"):
            logger.info("Converting legacy XLS file to XLSX: %s", card_path)
            temp_xls_path = os.path.join(
                job_dir, f"temp_{self.job_id}_{os.path.basename(card_path)}"
            )
            os.makedirs(job_dir, exist_ok=True)
            with open(temp_xls_path, "wb") as f:
                f.write(card_bytes)

            converted_xlsx_path = convert_xls_to_xlsx(temp_xls_path, output_dir=job_dir)
            if not converted_xlsx_path or not os.path.exists(converted_xlsx_path):
                raise ValueError(f"Failed to convert XLS file to XLSX: {card_path}")

            with open(converted_xlsx_path, "rb") as f:
                card_bytes = f.read()

            # Clean up temp converted files
            try:
                os.remove(temp_xls_path)
                os.remove(converted_xlsx_path)
            except OSError:
                pass

            filename = os.path.splitext(filename)[0] + ".xlsx"

        # 3. Classify card
        parser = CardParserService(mapping_config)
        classification = parser.classify(filename)
        logger.info(f"Card {card_path} classified as '{classification}'")

        # Destination paths
        dest_xlsx_path = os.path.join(
            translated_cards_dir, os.path.dirname(card_path), filename
        )
        os.makedirs(os.path.dirname(dest_xlsx_path), exist_ok=True)

        card_materials_path = os.path.join(
            card_materials_dir, os.path.dirname(card_path), f"{filename}.json"
        )
        os.makedirs(os.path.dirname(card_materials_path), exist_ok=True)

        if classification != "operational_card":
            # Non-operational card (service or unknown). Save original directly and skip translation/parsing.
            with open(dest_xlsx_path, "wb") as f:
                f.write(card_bytes)

            # Create an empty materials file so aggregate task doesn't fail
            with open(card_materials_path, "w", encoding="utf-8") as f:
                json.dump([], f)
            return

        # 3b. Handle Sheet Splitting for multi-sheet cards
        temp_source_path = os.path.join(job_dir, f"split_src_{self.job_id}_{filename}")
        os.makedirs(job_dir, exist_ok=True)
        with open(temp_source_path, "wb") as f:
            f.write(card_bytes)

        try:
            wb_check = openpyxl.load_workbook(temp_source_path, read_only=True)
            sheet_names = wb_check.sheetnames
            wb_check.close()
        except Exception as e:
            logger.warning(
                "Failed to check sheets with openpyxl, treating as single sheet: %s", e
            )
            sheet_names = []

        # Find sheets to split (exclude service sheets)
        sheets_to_split = []
        for sn in sheet_names:
            sn_lower = sn.lower()
            is_svc_sheet = any(
                kw.lower() in sn_lower for kw in _DEFAULT_SERVICE_KEYWORDS
            )
            if not is_svc_sheet:
                sheets_to_split.append(sn)

        split_files = []
        if len(sheets_to_split) > 1:
            logger.info(
                "Multi-sheet card detected. Splitting %d sheets: %s",
                len(sheets_to_split),
                sheets_to_split,
            )
            split_dir = os.path.join(
                job_dir, "split_cards", os.path.splitext(filename)[0]
            )
            splitter = CardSplitter(max_workers=1)
            try:
                split_files = splitter.split_file(
                    temp_source_path,
                    split_dir,
                    sheets_to_split,
                    file_label=os.path.splitext(filename)[0],
                )
            except Exception as e:
                logger.error("Failed to split card %s: %s", filename, e, exc_info=True)

        # Cleanup temp source file
        try:
            os.remove(temp_source_path)
        except OSError:
            pass

        # 4. Parse the operational card (single sheet or all split parts)
        if split_files:
            # Process each split file as a separate sub-card
            for sf in split_files:
                sf_name = os.path.basename(sf)
                logger.info("Processing split operational card: %s", sf_name)

                with open(sf, "rb") as f:
                    sf_bytes = f.read()

                sf_parse_result = parser.parse_card(sf_bytes, sf_name)
                if sf_parse_result.error:
                    logger.warning("Split file parse error: %s", sf_parse_result.error)
                    continue

                sf_card_no = extract_card_number(sf_name)
                sf_unique_chinese_texts = {
                    p.name for p in sf_parse_result.parts if p.name
                }

                # Translate
                sf_translations = {}
                if sf_unique_chinese_texts:
                    ml_client = StructureAdapter(self.settings.ml_service_url)
                    try:
                        sf_translations = ml_client.translate_batch(
                            list(sf_unique_chinese_texts)
                        )
                    except Exception as e:
                        logger.warning(
                            f"Translation batch failed for split card {sf_name}: {e}"
                        )

                sf_parts = []
                for p in sf_parse_result.parts:
                    sf_parts.append(
                        {
                            "card_no": sf_card_no,
                            "sheet_name": p.source_sheet,
                            "row_index": p.row,
                            "part_no": p.part_number,
                            "name_cn": p.name,
                            "name_ru": sf_translations.get(p.name, p.name),
                            "qty": p.quantity,
                        }
                    )

                # Save translated split workbook
                sf_dest_xlsx_path = os.path.join(
                    translated_cards_dir, os.path.dirname(card_path), sf_name
                )
                os.makedirs(os.path.dirname(sf_dest_xlsx_path), exist_ok=True)

                sf_name_col = (
                    mapping_config.get("cards", {}).get("columns", {}).get("name", 0)
                )
                sf_wb = openpyxl.load_workbook(sf, data_only=False)
                try:
                    for p in sf_parse_result.parts:
                        if p.name and sf_name_col > 0:
                            ws = sf_wb[p.source_sheet]
                            # Handle potential strike rows
                            cell = ws.cell(row=p.row, column=sf_name_col)
                            if cell.font and cell.font.strike:
                                continue
                            cell.value = sf_translations.get(p.name, p.name)
                    sf_wb.save(sf_dest_xlsx_path)
                finally:
                    sf_wb.close()

                # Save split parts list as JSON
                sf_card_materials_path = os.path.join(
                    card_materials_dir, os.path.dirname(card_path), f"{sf_name}.json"
                )
                os.makedirs(os.path.dirname(sf_card_materials_path), exist_ok=True)
                with open(sf_card_materials_path, "w", encoding="utf-8") as f:
                    json.dump(sf_parts, f, ensure_ascii=False, indent=2)

                # Cleanup temp split file
                try:
                    os.remove(sf)
                except OSError:
                    pass
        else:
            # Single-sheet mode
            parse_result = parser.parse_card(card_bytes, filename)
            if parse_result.error:
                raise ValueError(parse_result.error)

            card_no = extract_card_number(card_path)
            unique_chinese_texts = {p.name for p in parse_result.parts if p.name}

            # Translate Chinese names
            translations = {}
            if unique_chinese_texts:
                ml_client = StructureAdapter(self.settings.ml_service_url)
                try:
                    translations = ml_client.translate_batch(list(unique_chinese_texts))
                except Exception as e:
                    logger.warning(
                        f"Translation batch failed for {card_path}: {e}. Falling back to original texts."
                    )

            # Apply style-preserving updates and update parts
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

            # Save translated workbook
            name_col = mapping_config.get("cards", {}).get("columns", {}).get("name", 0)
            wb = openpyxl.load_workbook(io.BytesIO(card_bytes), data_only=False)
            try:
                for p in parse_result.parts:
                    if p.name and name_col > 0:
                        ws = wb[p.source_sheet]
                        cell = ws.cell(row=p.row, column=name_col)
                        if cell.font and cell.font.strike:
                            continue
                        cell.value = translations.get(p.name, p.name)
                wb.save(dest_xlsx_path)
            finally:
                wb.close()

            # Save parts list as JSON
            with open(card_materials_path, "w", encoding="utf-8") as f:
                json.dump(parts, f, ensure_ascii=False, indent=2)

    def write_error_file(self, card_path: str, exc: Exception) -> None:
        """Write error materials file containing exception detail."""
        storage_root = self.settings.storage_path
        job_dir = os.path.join(storage_root, str(self.job_id))
        card_materials_dir = os.path.join(job_dir, "card_materials")

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
