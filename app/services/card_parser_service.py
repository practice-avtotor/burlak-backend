"""ML-driven card parser service.

Replaces the heuristic-based ``burlak_parser.card_parser`` with a dynamic
parser that reads column coordinates and table boundaries from
``mapping_config`` (produced by the ML ``/analyze-structure`` endpoint).

Key design choices:
  - Operates entirely in-memory via ``io.BytesIO`` — no disk writes.
  - Uses the normalizers from :mod:`app.services.normalizer` for consistent
    part-number and quantity handling.
  - Classifies files by regex/keyword rules embedded in ``mapping_config``.

Typical usage inside a Celery task::

    from app.services.card_parser_service import CardParserService

    parser = CardParserService(mapping_config)
    result = parser.parse_card(xlsx_bytes, "001-card.xlsx")
"""

from __future__ import annotations

import io
import logging
import os
import re
from dataclasses import dataclass
from typing import Any

import openpyxl  # type: ignore[import-untyped]

from app.services.normalizer import (
    clean_part_number,
    is_valid_part_number,
    normalize_part_number,
    normalize_quantity,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ParsedPart:
    """A single material part extracted from an operational card."""

    part_number: str
    quantity: float
    name: str
    source_sheet: str
    row: int


@dataclass
class CardParseResult:
    """Result of parsing one operational card XLSX."""

    file_name: str
    file_type: str  # "operational_card" | "service" | "unknown"
    parts: list[ParsedPart]
    aggregated_parts: dict[str, float]  # normalized part_no → total qty
    original_part_numbers: dict[str, str]  # normalized → original form
    sheets_parsed: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

# Default classification rules (used when mapping_config omits them).
# These mirror the universal patterns from the original ``file_classifier.py``
# but are intentionally simpler — no dependency on ``heuristic_analyzer``.
_DEFAULT_SERVICE_KEYWORDS: list[str] = [
    "封面",
    "目录",
    "记录表",
    "空表",
    "填写范本",
    "填写说明",
    "工艺现场工时汇总清单",
    "工时汇总",
    "对比",
    "обложка",
    "содержание",
    "cover",
    "toc",
    "template",
]

_DEFAULT_OPERATIONAL_KEYWORDS: list[str] = [
    "作业指导书",
    "作业要领书",
    "操作指导",
    "工艺卡",
    "工序卡",
]


def classify_file(
    filename: str,
    classification_rules: dict[str, Any] | None = None,
) -> str:
    """Classify a file as ``service``, ``operational_card``, or ``unknown``.

    Uses rules from ``mapping_config.cards.file_classification_rules`` when
    provided, otherwise falls back to built-in keyword lists.

    Args:
        filename: Basename of the file (without directory path).
        classification_rules: Optional dict with keys
            ``service_keywords`` and ``operational_keywords``
            (each a list of strings).

    Returns:
        One of ``"service"``, ``"operational_card"``, ``"unknown"``.
    """
    name_lower = os.path.splitext(filename)[0].lower()

    svc_keywords = _DEFAULT_SERVICE_KEYWORDS
    op_keywords = _DEFAULT_OPERATIONAL_KEYWORDS

    if classification_rules:
        svc_keywords = classification_rules.get("service_keywords", svc_keywords)
        op_keywords = classification_rules.get("operational_keywords", op_keywords)

    # Service keywords take priority (highest precedence)
    for kw in svc_keywords:
        if kw.lower() in name_lower:
            return "service"

    # Operational card keywords
    for kw in op_keywords:
        if kw.lower() in name_lower:
            return "operational_card"

    # Heuristic: filename starts with 2+ digits (operation number)
    if re.match(r"^(?:[A-Za-z]{1,3})?\d{2,}", name_lower):
        return "operational_card"

    # Heuristic: pattern like MODEL-A-AS-NNNNN
    if re.match(r"^[a-z0-9]+-[a-z0-9]*-as-\d+", name_lower):
        return "operational_card"

    return "unknown"


# ---------------------------------------------------------------------------
# Card parser service
# ---------------------------------------------------------------------------


class CardParserService:
    """Parses operational card XLSX files using ML-provided mapping config.

    The mapping config is produced by ``StructureAdapter.analyze_structure``
    and typically looks like::

        {
            "cards": {
                "file_classification_rules": { ... },
                "table_boundaries": {
                    "header_row": 1,
                    "data_start_row": 2,
                    "end_markers": ["签字", "审核"],
                },
                "columns": {
                    "part_no": 1,
                    "qty": 3,
                    "name": 2,
                },
                "sheets": {
                    "type_column": null,
                    "default_type": "operational",
                },
            }
        }

    Args:
        mapping_config: The full mapping config dict from the ML service.
    """

    def __init__(self, mapping_config: dict[str, Any]) -> None:
        cards_cfg = mapping_config.get("cards", {})
        self._classification_rules: dict[str, Any] | None = cards_cfg.get(
            "file_classification_rules"
        )
        self._table_boundaries: dict[str, Any] = cards_cfg.get("table_boundaries", {})
        self._columns: dict[str, int] = cards_cfg.get("columns", {})
        self._sheets_cfg: dict[str, Any] = cards_cfg.get("sheets", {})

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, filename: str) -> str:
        """Classify a file using the ML-provided rules.

        Returns:
            ``"service"``, ``"operational_card"``, or ``"unknown"``.
        """
        return classify_file(filename, self._classification_rules)

    def parse_card(self, data: bytes, filename: str) -> CardParseResult:
        """Parse an operational card XLSX from raw bytes.

        Args:
            data: Raw bytes of the XLSX file.
            filename: Original filename (used for classification and logging).

        Returns:
            :class:`CardParseResult` with extracted parts.

        Raises:
            ValueError: If the file cannot be opened as XLSX.
        """
        file_type = self.classify(filename)
        if file_type == "service":
            return CardParseResult(
                file_name=filename,
                file_type="service",
                parts=[],
                aggregated_parts={},
                original_part_numbers={},
            )
        if file_type == "unknown":
            logger.warning("Unknown file type, skipping: %s", filename)
            return CardParseResult(
                file_name=filename,
                file_type="unknown",
                parts=[],
                aggregated_parts={},
                original_part_numbers={},
            )

        return self._parse_operational_card(data, filename)

    def extract_unique_strings(self, data: bytes, filename: str) -> list[str]:
        """Extract unique translatable strings from an operational card.

        Reads the name column (and any other text columns) to build a
        deduplicated list of Chinese strings suitable for batch translation.

        Args:
            data: Raw bytes of the XLSX file.
            filename: Original filename.

        Returns:
            Deduplicated list of non-empty strings from the name column.
        """
        file_type = self.classify(filename)
        if file_type != "operational_card":
            return []

        name_col = self._columns.get("name", 0)
        if name_col <= 0:
            return []

        header_row = self._table_boundaries.get("header_row", 1)
        data_start = self._table_boundaries.get("data_start_row", header_row + 1)
        end_markers: list[str] = self._table_boundaries.get("end_markers", [])

        seen: set[str] = set()
        result: list[str] = []

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                for row in ws.iter_rows(min_row=data_start, max_col=name_col):
                    if not row or len(row) < name_col:
                        continue
                    cell = row[name_col - 1]
                    val = cell.value
                    if val is None:
                        continue
                    text = str(val).strip()
                    if not text:
                        continue
                    # Stop at end markers
                    if any(marker in text for marker in end_markers):
                        break
                    if text not in seen:
                        seen.add(text)
                        result.append(text)
        finally:
            wb.close()

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_operational_card(self, data: bytes, filename: str) -> CardParseResult:
        """Core parsing logic for operational card files."""
        part_no_col = self._columns.get("part_no", 0)
        qty_col = self._columns.get("qty", 0)
        name_col = self._columns.get("name", 0)
        header_row = self._table_boundaries.get("header_row", 1)
        data_start = self._table_boundaries.get("data_start_row", header_row + 1)
        end_markers: list[str] = self._table_boundaries.get("end_markers", [])

        if part_no_col <= 0:
            return CardParseResult(
                file_name=filename,
                file_type="operational_card",
                parts=[],
                aggregated_parts={},
                original_part_numbers={},
                error="mapping_config missing part_no column index",
            )

        parts: list[ParsedPart] = []
        aggregated: dict[str, float] = {}
        original_pns: dict[str, str] = {}
        sheets_parsed = 0

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                sheet_parts = self._extract_parts_from_sheet(
                    ws,
                    sheet_name,
                    part_no_col,
                    qty_col,
                    name_col,
                    data_start,
                    end_markers,
                )
                if sheet_parts:
                    sheets_parsed += 1
                    for p in sheet_parts:
                        parts.append(p)
                        norm_pn = normalize_part_number(p.part_number)
                        aggregated[norm_pn] = aggregated.get(norm_pn, 0.0) + p.quantity
                        if norm_pn not in original_pns:
                            original_pns[norm_pn] = p.part_number
        finally:
            wb.close()

        return CardParseResult(
            file_name=filename,
            file_type="operational_card",
            parts=parts,
            aggregated_parts=aggregated,
            original_part_numbers=original_pns,
            sheets_parsed=sheets_parsed,
        )

    def _extract_parts_from_sheet(
        self,
        ws: openpyxl.worksheet.worksheet.Worksheet,
        sheet_name: str,
        part_no_col: int,
        qty_col: int,
        name_col: int,
        data_start: int,
        end_markers: list[str],
    ) -> list[ParsedPart]:
        """Extract material parts from a single worksheet.

        Reads rows starting from ``data_start`` until an end marker is
        encountered or the sheet is exhausted.
        """
        parts: list[ParsedPart] = []
        consecutive_empty = 0

        # read_only worksheets expose max_row via ws.max_row
        max_row = ws.max_row or 0

        for row_idx in range(data_start, max_row + 1):
            try:
                # Read the part number cell
                raw_pn = self._cell_value(ws, row_idx, part_no_col)

                # Check end markers
                if raw_pn is not None:
                    pn_str = str(raw_pn).strip()
                    if any(marker in pn_str for marker in end_markers):
                        break

                # Handle empty rows
                if raw_pn is None:
                    # Also check name column for end markers
                    if name_col > 0:
                        raw_name = self._cell_value(ws, row_idx, name_col)
                        if raw_name is not None:
                            name_str = str(raw_name).strip()
                            if any(marker in name_str for marker in end_markers):
                                break
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                    continue

                consecutive_empty = 0

                pn_str = str(raw_pn).strip()
                if not pn_str:
                    continue

                # Validate part number
                cleaned_pn = clean_part_number(pn_str)
                if not is_valid_part_number(cleaned_pn, strict=True):
                    continue

                # Quantity
                qty = 1.0
                if qty_col > 0:
                    raw_qty = self._cell_value(ws, row_idx, qty_col)
                    qty = normalize_quantity(raw_qty, default=1.0)
                    if qty <= 0:
                        qty = 1.0

                # Name
                name = ""
                if name_col > 0:
                    raw_name = self._cell_value(ws, row_idx, name_col)
                    name = str(raw_name).strip() if raw_name is not None else ""

                # Store raw PN in ParsedPart so callers can access
                # the original formatting (dashes, etc.)
                parts.append(
                    ParsedPart(
                        part_number=pn_str,
                        quantity=qty,
                        name=name,
                        source_sheet=sheet_name,
                        row=row_idx,
                    )
                )

            except Exception as e:
                logger.debug(
                    "Error parsing row %d in sheet '%s': %s",
                    row_idx,
                    sheet_name,
                    e,
                )
                continue

        return parts

    @staticmethod
    def _cell_value(
        ws: openpyxl.worksheet.worksheet.Worksheet, row: int, col: int
    ) -> Any:
        """Read a cell value from an openpyxl worksheet.

        Returns ``None`` for out-of-bounds access instead of raising.
        """
        try:
            return ws.cell(row=row, column=col).value
        except (AttributeError, IndexError, KeyError):
            return None
