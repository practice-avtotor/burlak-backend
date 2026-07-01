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

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

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
class MLCardParseResult:
    """Result of parsing one operational card XLSX via ML-driven config.

    Renamed from ``CardParseResult`` to avoid collision with
    :class:`app.schemas.cards.CardParseResult` (different schema).
    """

    file_name: str
    file_type: str  # "operational_card" | "service" | "unknown"
    parts: list[ParsedPart]
    aggregated_parts: dict[str, float]  # normalized part_no → total qty
    original_part_numbers: dict[str, str]  # normalized → original form
    sheets_parsed: int = 0
    error: str | None = None
    card_boundaries: list[tuple[str, int, int]] | None = None
    """For multi-card sheets: list of (sheet_name, start_row, end_row) for each
    card found.  ``None`` for single-card sheets."""


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

    .. note::

        This function returns only the file type string.  If you need the
        ``format_group`` name (for the new ``cards.formats`` schema), use
        :func:`classify_file_with_format` instead.

    Args:
        filename: Basename of the file (without directory path).
        classification_rules: Optional dict with keys
            ``service_keywords`` and ``operational_keywords``
            (each a list of strings), **or** the new schema keys
            ``operational_card_patterns`` and ``service_file_patterns``
            (each a list of pattern dicts).

    Returns:
        One of ``"service"``, ``"operational_card"``, ``"unknown"``.
    """
    file_type, _ = classify_file_with_format(filename, classification_rules)
    return file_type


def classify_file_with_format(
    filename: str,
    classification_rules: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Classify a file and return ``(file_type, format_group)``.

    Supports both the legacy flat schema (``service_keywords`` /
    ``operational_keywords``) and the new schema
    (``operational_card_patterns`` / ``service_file_patterns`` with
    ``format_group``).

    Args:
        filename: Basename of the file (without directory path).
        classification_rules: Optional dict from
            ``mapping_config.cards.file_classification_rules``.

    Returns:
        Tuple of ``(file_type, format_group)`` where ``file_type`` is one of
        ``"service"``, ``"operational_card"``, ``"unknown"`` and
        ``format_group`` is the matched format name (e.g. ``"card_format_A"``)
        or ``None`` for service/unknown files or when using legacy rules.
    """
    name_lower = os.path.splitext(filename)[0].lower()

    # ── Detect schema type ──────────────────────────────────────────
    # New schema: has "operational_card_patterns" or "service_file_patterns"
    # Legacy schema: has "service_keywords" or "operational_keywords"
    has_new_schema = bool(
        classification_rules
        and (
            classification_rules.get("operational_card_patterns")
            or classification_rules.get("service_file_patterns")
        )
    )

    if has_new_schema:
        return _classify_with_new_schema(name_lower, classification_rules)
    else:
        return _classify_with_legacy_schema(name_lower, classification_rules)


def _classify_with_new_schema(
    name_lower: str,
    rules: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Classify using the new ``operational_card_patterns`` /
    ``service_file_patterns`` schema.
    """
    if not rules:
        return "unknown", None

    # ── Service file patterns (highest priority) ────────────────────
    svc_patterns: list[dict[str, Any]] = rules.get("service_file_patterns", [])
    for pattern_def in svc_patterns:
        ptype = pattern_def.get("type", "")
        if ptype == "filename_keyword":
            keywords: list[str] = pattern_def.get("keywords", [])
            for kw in keywords:
                if kw.lower() in name_lower:
                    return "service", None
        elif ptype == "filename_regex":
            regex = pattern_def.get("pattern", "")
            if regex and re.search(regex, name_lower, re.IGNORECASE):
                return "service", None
        elif ptype == "sheet_keyword":
            # sheet_keyword patterns are checked against the filename
            # as a heuristic fallback; if the filename itself contains
            # any of the keywords, classify as service.
            keywords = pattern_def.get("keywords", [])
            for kw in keywords:
                if kw.lower() in name_lower:
                    return "service", None

    # ── Operational card patterns ───────────────────────────────────
    op_patterns: list[dict[str, Any]] = rules.get("operational_card_patterns", [])
    for pattern_def in op_patterns:
        ptype = pattern_def.get("type", "")
        format_group: str | None = pattern_def.get("format_group")

        if ptype == "filename_regex":
            regex = pattern_def.get("pattern", "")
            if regex and re.search(regex, name_lower, re.IGNORECASE):
                return "operational_card", format_group

        elif ptype == "filename_keyword":
            keywords = pattern_def.get("keywords", [])
            for kw in keywords:
                if kw.lower() in name_lower:
                    return "operational_card", format_group

        elif ptype == "sheet_keyword":
            keywords = pattern_def.get("keywords", [])
            for kw in keywords:
                if kw.lower() in name_lower:
                    return "operational_card", format_group

    # ── Heuristic fallbacks (same as legacy) ────────────────────────
    if re.match(r"^(?:[A-Za-z]{1,3})?\d{2,}", name_lower):
        return "operational_card", None

    if re.match(r"^[a-z0-9]+-[a-z0-9]*-as-\d+", name_lower):
        return "operational_card", None

    return "unknown", None


def _classify_with_legacy_schema(
    name_lower: str,
    rules: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Classify using the legacy flat ``service_keywords`` /
    ``operational_keywords`` schema.
    """
    svc_keywords = _DEFAULT_SERVICE_KEYWORDS
    op_keywords = _DEFAULT_OPERATIONAL_KEYWORDS

    if rules:
        svc_keywords = rules.get("service_keywords", svc_keywords)
        op_keywords = rules.get("operational_keywords", op_keywords)

    # Service keywords take priority (highest precedence)
    for kw in svc_keywords:
        if kw.lower() in name_lower:
            return "service", None

    # Operational card keywords
    for kw in op_keywords:
        if kw.lower() in name_lower:
            return "operational_card", None

    # Heuristic: filename starts with 2+ digits (operation number)
    if re.match(r"^(?:[A-Za-z]{1,3})?\d{2,}", name_lower):
        return "operational_card", None

    # Heuristic: pattern like MODEL-A-AS-NNNNN
    if re.match(r"^[a-z0-9]+-[a-z0-9]*-as-\d+", name_lower):
        return "operational_card", None

    return "unknown", None


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

        # ── Store all formats keyed by format name ──────────────────────
        # New format: cards.formats.card_format_A.sheets[0].{columns, table_boundaries}
        # Old format: cards.{columns, table_boundaries}
        self._formats: dict[str, dict[str, Any]] | None = None
        self._flat_tb: dict[str, Any] | None = None
        self._flat_columns: dict[str, Any] | None = None

        formats = cards_cfg.get("formats")
        if isinstance(formats, dict):
            self._formats = {}
            for fmt_name, fmt in formats.items():
                sheets = fmt.get("sheets", [])
                if sheets:
                    sheet0 = sheets[0]
                    tb = dict(sheet0.get("table_boundaries", {}))
                    if "header_rows" not in tb and sheet0.get("header_rows") is not None:
                        tb["header_rows"] = sheet0["header_rows"]
                    if "data_start_row" not in tb and sheet0.get("data_start_row") is not None:
                        tb["data_start_row"] = sheet0["data_start_row"]
                    columns = sheet0.get("columns", {})
                    if not tb.get("end_markers") and fmt.get("end_markers"):
                        tb["end_markers"] = fmt["end_markers"]
                    self._formats[fmt_name] = {
                        "table_boundaries": tb,
                        "columns": columns,
                    }
        else:
            self._flat_tb = cards_cfg.get("table_boundaries", {})
            self._flat_columns = cards_cfg.get("columns", {})

        # ── Resolve initial config (for legacy flat schema) ─────────────
        tb, columns = self._resolve_format_config(None)

        self._table_boundaries: dict[str, Any] = tb

        # ML boundary schema adapter: handle list or single integer for header_row
        header_rows = tb.get("header_rows", [1])
        if isinstance(header_rows, list) and header_rows:
            self._header_row = header_rows[0] if isinstance(header_rows[0], int) else 1
        elif isinstance(header_rows, int):
            self._header_row = header_rows
        else:
            self._header_row = tb.get("header_row", 1)

        # ML column schema adapter: handle nested dictionaries (e.g. {"col_index": 1})
        # Also map name_cn → name for backward compatibility
        self._columns = self._normalise_columns(columns)

        # Plain attribute for name column index (used by card_processing_service)
        val = self._columns.get("name", 0)
        self.name_col: int = int(val) if val else 0

        self._sheets_cfg: dict[str, Any] = cards_cfg.get("sheets", {})

        # Multi-card configuration (from table_boundaries.multi_card)
        self._multi_card_cfg: dict[str, Any] | None = tb.get("multi_card")
        self._is_multi_card = tb.get("type") == "multi_card" and bool(self._multi_card_cfg)
        self._last_card_boundaries: list[tuple[str, int, int]] | None = None

    # ------------------------------------------------------------------
    # Format resolution
    # ------------------------------------------------------------------

    def _resolve_format_config(
        self,
        format_group: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Resolve ``(table_boundaries, columns)`` for a given format group.

        If ``format_group`` is specified and exists in ``self._formats``,
        returns that format's config.  Otherwise falls back to the legacy
        flat ``cards.table_boundaries`` / ``cards.columns``.

        Returns:
            Tuple of ``(table_boundaries, columns)`` dicts.
        """
        if (
            self._formats
            and format_group
            and format_group in self._formats
        ):
            fmt = self._formats[format_group]
            return fmt["table_boundaries"], fmt["columns"]

        # Fall back to flat config
        if self._flat_tb is not None and self._flat_columns is not None:
            return self._flat_tb, self._flat_columns

        # Last resort: empty configs
        return {}, {}

    @staticmethod
    def _normalise_columns(columns: dict[str, Any]) -> dict[str, int]:
        """Normalise column dict values to plain integer indices.

        Handles both ``{"part_no": 1}`` and ``{"part_no": {"col_index": 1}}``.
        Also aliases ``name_cn`` → ``name`` for backward compatibility.
        """
        result: dict[str, int] = {}
        for key, val in columns.items():
            if isinstance(val, dict):
                result[key] = val.get("col_index", 0)
            else:
                result[key] = int(val) if val else 0
        # If name_cn is present but name is not, alias name_cn → name
        if "name_cn" in result and "name" not in result:
            result["name"] = result["name_cn"]
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, filename: str) -> str:
        """Classify a file using the ML-provided rules.

        Returns:
            ``"service"``, ``"operational_card"``, or ``"unknown"``.
        """
        return classify_file(filename, self._classification_rules)

    def classify_with_format(self, filename: str) -> tuple[str, str | None]:
        """Classify a file and return ``(file_type, format_group)``.

        The ``format_group`` is the matched format name (e.g.
        ``"card_format_A"``) from ``file_classification_rules``, or
        ``None`` for service/unknown files or when using legacy rules.
        """
        return classify_file_with_format(filename, self._classification_rules)

    def parse_card(self, data: bytes, filename: str) -> MLCardParseResult:
        """Parse an operational card XLSX from raw bytes.

        Args:
            data: Raw bytes of the XLSX file.
            filename: Original filename (used for classification and logging).

        Returns:
            :class:`MLCardParseResult` with extracted parts.

        Raises:
            ValueError: If the file cannot be opened as XLSX.
        """
        file_type, format_group = self.classify_with_format(filename)
        if file_type == "service":
            return MLCardParseResult(
                file_name=filename,
                file_type="service",
                parts=[],
                aggregated_parts={},
                original_part_numbers={},
            )
        if file_type == "unknown":
            logger.warning("Unknown file type, skipping: %s", filename)
            return MLCardParseResult(
                file_name=filename,
                file_type="unknown",
                parts=[],
                aggregated_parts={},
                original_part_numbers={},
            )

        # Resolve format-specific config for this file
        tb, columns = self._resolve_format_config(format_group)
        return self._parse_operational_card(data, filename, tb, columns)

    # ------------------------------------------------------------------
    # Internal helpers

    # ------------------------------------------------------------------

    def _parse_operational_card(
        self,
        data: bytes,
        filename: str,
        tb: dict[str, Any] | None = None,
        columns: dict[str, Any] | None = None,
    ) -> MLCardParseResult:
        """Core parsing logic for operational card files.

        Args:
            data: Raw XLSX bytes.
            filename: Original filename (for logging).
            tb: Table boundaries dict for the resolved format.
                Falls back to ``self._table_boundaries`` if ``None``.
            columns: Columns dict for the resolved format (may contain
                nested ``{"col_index": N}`` values).
                Falls back to ``self._columns`` if ``None``.
        """
        tb = tb if tb is not None else self._table_boundaries
        columns = columns if columns is not None else self._columns

        # Normalise columns (handle nested {"col_index": N} values)
        normalised = self._normalise_columns(columns)

        part_no_col = normalised.get("part_no", 0)
        qty_col = normalised.get("qty", 0)
        name_col = normalised.get("name", 0)

        # Resolve header_row from the format-specific tb
        header_rows = tb.get("header_rows", [1])
        if isinstance(header_rows, list) and header_rows:
            header_row = header_rows[0] if isinstance(header_rows[0], int) else 1
        elif isinstance(header_rows, int):
            header_row = header_rows
        else:
            header_row = tb.get("header_row", 1)

        data_start = tb.get("data_start_row", header_row + 1)
        end_markers: list[str] = tb.get("end_markers", [])

        if part_no_col <= 0:
            return MLCardParseResult(
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
        validation_error: str | None = None
        # Reset multi-card boundaries at the start of each parse_card call
        # so single-card sheets don't inherit stale boundaries from a previous
        # multi-card sheet in the same workbook.
        self._last_card_boundaries = None

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                # Validate column indices against max_column
                if ws.max_column:
                    if part_no_col > ws.max_column:
                        validation_error = f"part_no column index {part_no_col} exceeds sheet max column {ws.max_column}"
                        break
                    if qty_col > 0 and qty_col > ws.max_column:
                        validation_error = f"qty column index {qty_col} exceeds sheet max column {ws.max_column}"
                        break
                    if name_col > 0 and name_col > ws.max_column:
                        validation_error = f"name column index {name_col} exceeds sheet max column {ws.max_column}"
                        break

                sheet_parts = self._extract_parts_from_sheet(
                    ws,
                    sheet_name,
                    part_no_col,
                    qty_col,
                    name_col,
                    data_start,
                    end_markers,
                    table_boundaries=tb,
                )
                if sheet_parts:
                    sheets_parsed += 1
                    for p in sheet_parts:
                        parts.append(p)
                        norm_pn = normalize_part_number(p.part_number)
                        aggregated[norm_pn] = aggregated.get(norm_pn, 0.0) + p.quantity
                        if norm_pn not in original_pns:
                            original_pns[norm_pn] = p.part_number

            # Check if 0 parts were parsed from sheets that have substantial content
            if not parts and not validation_error:
                has_content = False
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    non_empty_rows = 0
                    max_scan = min(ws.max_row or 0, 100)
                    max_col_check = min(ws.max_column or 0, 20)
                    for r in range(1, max_scan + 1):
                        row_has_val = False
                        for c in range(1, max_col_check + 1):
                            try:
                                v = ws.cell(row=r, column=c).value
                                if v is not None and str(v).strip():
                                    row_has_val = True
                                    break
                            except Exception:
                                pass
                        if row_has_val:
                            non_empty_rows += 1
                            if non_empty_rows >= 10:
                                has_content = True
                                break
                    if has_content:
                        break

                if has_content:
                    validation_error = "No parts extracted from non-empty worksheet. Check mapping config columns."
        finally:
            wb.close()

        return MLCardParseResult(
            file_name=filename,
            file_type="operational_card",
            parts=parts,
            aggregated_parts=aggregated,
            original_part_numbers=original_pns,
            sheets_parsed=sheets_parsed,
            error=validation_error,
            card_boundaries=self._last_card_boundaries,
        )

    def _extract_parts_from_sheet(
        self,
        ws: Worksheet,
        sheet_name: str,
        part_no_col: int,
        qty_col: int,
        name_col: int,
        data_start: int,
        end_markers: list[str],
        table_boundaries: dict[str, Any] | None = None,
    ) -> list[ParsedPart]:
        """Extract material parts from a single worksheet.

        Reads rows starting from ``data_start`` until an end marker is
        encountered or the sheet is exhausted.

        If ``table_boundaries.type == "multi_card"``, delegates to
        :meth:`_extract_parts_multi_card` which iterates over all cards
        in the sheet, separated by empty rows.
        """
        # Dispatch to multi-card handler if configured
        if table_boundaries and table_boundaries.get("type") == "multi_card":
            multi_card_cfg = table_boundaries.get("multi_card")
            if multi_card_cfg:
                parts, boundaries = self._extract_parts_multi_card(
                    ws, sheet_name, part_no_col, qty_col, name_col,
                    data_start, multi_card_cfg,
                )
                # Store boundaries on the instance so _parse_operational_card
                # can read them when building MLCardParseResult
                if self._last_card_boundaries is None:
                    self._last_card_boundaries = []
                self._last_card_boundaries.extend(boundaries)
                return parts

        sheet_parts: list[ParsedPart] = []
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

                # Check for strikethrough
                try:
                    cell = ws.cell(row=row_idx, column=part_no_col)
                    if cell and cell.font and cell.font.strike:
                        continue
                except Exception:
                    pass

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
                sheet_parts.append(
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

        return sheet_parts

    # ------------------------------------------------------------------
    # Multi-card support
    # ------------------------------------------------------------------

    def _extract_parts_multi_card(
        self,
        ws: Worksheet,
        sheet_name: str,
        part_no_col: int,
        qty_col: int,
        name_col: int,
        data_start: int,
        multi_card_cfg: dict[str, Any],
    ) -> tuple[list[ParsedPart], list[tuple[str, int, int]]]:
        """Extract parts from multiple cards stacked vertically in one sheet.

        Cards are separated by empty rows (configurable via
        ``multi_card_cfg.empty_rows_separator``).  Each card may have a
        repeating header that is skipped.

        Args:
            ws: openpyxl worksheet.
            sheet_name: Name of the sheet (for logging).
            part_no_col: 1-based column index for part numbers.
            qty_col: 1-based column index for quantities (0 = none).
            name_col: 1-based column index for names (0 = none).
            data_start: Row to start scanning from (1-based).
            multi_card_cfg: The ``multi_card`` sub-dict from
                ``table_boundaries``.

        Returns:
            Tuple of (parts, card_boundaries) where:
            - parts: List of :class:`ParsedPart` from all cards.
            - card_boundaries: List of (sheet_name, start_row, end_row) for
              each card.
        """
        empty_rows_sep = multi_card_cfg.get("empty_rows_separator", 1)
        parts_data_start = multi_card_cfg.get("parts_data_start_row", 0)
        parts_header_row = multi_card_cfg.get("parts_header_row", 0)
        card_end_markers: list[str] = multi_card_cfg.get("card_end_markers", [])
        max_cards = multi_card_cfg.get("max_cards", 0)

        max_row = ws.max_row or 0
        row_idx = data_start
        cards_found = 0
        parts: list[ParsedPart] = []
        card_boundaries: list[tuple[str, int, int]] = []

        while row_idx <= max_row:
            # Skip empty rows (separators between cards)
            while row_idx <= max_row and self._is_row_empty(ws, row_idx, part_no_col, name_col):
                row_idx += 1

            if row_idx > max_row:
                break

            if max_cards > 0 and cards_found >= max_cards:
                break

            # Found the start of a card
            card_start = row_idx

            # Determine where the parts table starts within this card.
            # parts_data_start_row is a 1-based relative offset from card_start.
            #   parts_data_start_row=1 → data starts at card_start (first row of card)
            #   parts_data_start_row=2 → data starts at card_start + 1 (skip 1 header row)
            # If parts_data_start_row is absent, fall back to parts_header_row + 1.
            if parts_data_start > 0:
                actual_data_start = card_start + parts_data_start - 1
            elif parts_header_row > 0:
                actual_data_start = card_start + parts_header_row
            else:
                # Default: skip 1 row (assume header at card_start)
                actual_data_start = card_start + 1

            # Find the end of this card (empty row or end marker)
            card_end = self._find_card_end(
                ws, actual_data_start, max_row,
                empty_rows_sep, card_end_markers,
                part_no_col, name_col,
            )

            # Record boundary with sheet name for cross-sheet safety
            card_boundaries.append((sheet_name, card_start, card_end))

            # Extract parts from this card
            for r in range(actual_data_start, card_end + 1):
                raw_pn = self._cell_value(ws, r, part_no_col)
                if raw_pn is None:
                    continue

                pn_str = str(raw_pn).strip()
                if not pn_str:
                    continue

                # Check for strikethrough
                try:
                    cell = ws.cell(row=r, column=part_no_col)
                    if cell and cell.font and cell.font.strike:
                        continue
                except Exception:
                    pass

                # Validate part number
                cleaned_pn = clean_part_number(pn_str)
                if not is_valid_part_number(cleaned_pn, strict=True):
                    continue

                # Quantity
                qty = 1.0
                if qty_col > 0:
                    raw_qty = self._cell_value(ws, r, qty_col)
                    qty = normalize_quantity(raw_qty, default=1.0)
                    if qty <= 0:
                        qty = 1.0

                # Name
                name = ""
                if name_col > 0:
                    raw_name = self._cell_value(ws, r, name_col)
                    name = str(raw_name).strip() if raw_name is not None else ""

                parts.append(
                    ParsedPart(
                        part_number=pn_str,
                        quantity=qty,
                        name=name,
                        source_sheet=sheet_name,
                        row=r,
                    )
                )

            cards_found += 1
            row_idx = card_end + 1

            # Skip end marker row if present (it was detected by _find_card_end
            # which returned card_end = marker_row - 1, so row_idx = marker_row)
            if row_idx <= max_row:
                raw_check = self._cell_value(ws, row_idx, part_no_col)
                if raw_check is not None:
                    check_str = str(raw_check).strip()
                    if any(m in check_str for m in card_end_markers):
                        row_idx += 1

        logger.info(
            "Multi-card sheet '%s': found %d card(s), extracted %d part(s)",
            sheet_name, cards_found, len(parts),
        )
        return parts, card_boundaries

    @staticmethod
    def _is_row_empty(ws: Worksheet, row: int, col1: int, col2: int) -> bool:
        """Check if a row is empty in the given columns.

        Returns ``True`` if both ``col1`` and ``col2`` are ``None`` or blank.
        """
        v1 = CardParserService._cell_value(ws, row, col1)
        if v1 is not None and str(v1).strip():
            return False

        if col2 > 0 and col2 != col1:
            v2 = CardParserService._cell_value(ws, row, col2)
            if v2 is not None and str(v2).strip():
                return False

        return True

    @staticmethod
    def _find_card_end(
        ws: Worksheet,
        start_row: int,
        max_row: int,
        empty_rows_sep: int,
        end_markers: list[str],
        part_no_col: int,
        name_col: int,
    ) -> int:
        """Find the last data row of the current card.

        Scans from ``start_row`` upward until it finds a separator
        (empty rows or end marker).  Returns the last row that belongs
        to the card.
        """
        consecutive_empty = 0

        for row_idx in range(start_row, max_row + 1):
            raw_pn = CardParserService._cell_value(ws, row_idx, part_no_col)

            # Check end markers in part_no column
            if raw_pn is not None:
                pn_str = str(raw_pn).strip()
                if any(marker in pn_str for marker in end_markers):
                    return row_idx - 1

            # Check end markers in name column
            if name_col > 0 and name_col != part_no_col:
                raw_name = CardParserService._cell_value(ws, row_idx, name_col)
                if raw_name is not None:
                    name_str = str(raw_name).strip()
                    if any(marker in name_str for marker in end_markers):
                        return row_idx - 1

            # Count consecutive empty rows
            if CardParserService._is_row_empty(ws, row_idx, part_no_col, name_col):
                consecutive_empty += 1
                if consecutive_empty >= empty_rows_sep:
                    return row_idx - empty_rows_sep
            else:
                consecutive_empty = 0

        return max_row

    @staticmethod
    def _cell_value(ws: Worksheet, row: int, col: int) -> Any:
        """Read a cell value from an openpyxl worksheet.

        Returns ``None`` for out-of-bounds access instead of raising.
        """
        try:
            return ws.cell(row=row, column=col).value
        except (AttributeError, IndexError, KeyError):
            return None
