"""
Heuristic helper utilities for Excel parsing.
Contains shared keywords, regexes, and coordinate boundary search methods
used by splitter, BOM parser, and card processing services.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from app.services.normalizer import (
    clean_part_number,
    is_valid_part_number,
)

logger = logging.getLogger(__name__)

# Synonym dictionaries for column detection and validation
PART_NO_KEYWORDS: list[str] = [
    # Chinese
    "零件号",
    "零部件件号",
    "零件编码",
    "物料编码",
    "件号",
    "物料号",
    "料号",
    "零部件代号",
    "代号",
    "代码",
    "编码",
    "物料代码",
    "零件号(中文）",
    "零件号(中文)",
    "零件号（中文）",
    # English
    "part no",
    "partno",
    "part number",
    "part_no",
    "part#",
    "part number(中文）",
    "part number(中文)",
    "item code",
    "material code",
    "material no",
    "material number",
    "component code",
    "component number",
    "code",
    # Russian
    "код детали",
    "номер детали",
    "деталь",
    "код",
    "артикул",
    "каталожный номер",
]

NAME_KEYWORDS: list[str] = [
    # Chinese
    "零件名称",
    "零部件名称",
    "物料名称",
    "材料名称",
    "零部件名称(中文）",
    "零部件名称(中文)",
    "零件名称(中文)",
    "名称",
    "名称(中文）",
    "名称(中文)",
    "物料描述",
    "零件描述",
    "零部件描述",
    "零件中文名称",
    # English
    "part name",
    "partname",
    "part description",
    "description",
    "material description",
    "component name",
    "component description",
    "name",
    # Russian
    "наименование детали",
    "наименование",
    "описание детали",
    "описание",
    "название",
]

NAME_ANTI_KEYWORDS: list[str] = [
    "厂家",
    "供应商",
    "производитель",
    "поставщик",
    "supplier",
    "manufacturer",
]

QTY_KEYWORDS: list[str] = [
    # Chinese
    "数量",
    "用量",
    "单车用量",
    "组件数量",
    "配额",
    "单车",
    "件数",
    "额度",
    # English
    "qty",
    "quantity",
    "amount",
    "usage",
    "per vehicle",
    "count",
    # Russian
    "кол-во",
    "колво",
    "количество",
    "расход",
]

QTY_ANTI_KEYWORDS: list[str] = [
    "序号",
    "no",
    "no.",
    "n.",
    "index",
    "id",
    "штрих",
]

# Patterns for extracting operational card numbers
CARD_NUMBER_RE = re.compile(
    r"(?:"
    r"  [A-Za-z]+\d*[A-Za-z]*(?:-\d+)+[\w-]*"  # ABC1L-17-AS-04001
    r"|"
    r"  [A-Za-z]{1,3}\d{3,}[\w-]*"  # A001, G01
    r"|"
    r"  (?:TP|процесс|операция|card)\s*[-]?\s*\d+"  # TP-123
    r")",
    re.IGNORECASE | re.VERBOSE,
)

OP_NUMBER_IN_FILENAME_RE = re.compile(r"(?:^|[-_\s])(\d{2,})(?:[-_\s]|$)")
LETTERS_DIGITS_RE = re.compile(r"^([A-Za-z]{1,3}\d{2,})")
PREFIX_AS_RE = re.compile(
    r"^([A-Za-z0-9]+[-_])?[A-Za-z]?[-_]?AS[-_]?\d+",
    re.IGNORECASE,
)

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
XML_HEX_RE = re.compile(r"_x[0-9a-fA-F]{4}_")
XML_ARTIFACTS_RE = re.compile(r"(_x[0-9a-fA-F]{4}_|\r\n|[\r\n])", re.IGNORECASE)


def clean_cell_text(text: Any) -> str:
    """Clean cell text from Excel XML encoding artefacts.
    Takes only the FIRST part when separated by newline.
    """
    if text is None:
        return ""
    s = str(text).strip()
    match = XML_ARTIFACTS_RE.search(s)
    if match:
        s = s[: match.start()]
    return s.strip()


def extract_card_number_from_filepath(file_path: str) -> str:
    """Extract the operational card number from a file path."""
    basename = os.path.basename(file_path)
    name_no_ext = os.path.splitext(basename)[0]

    # Attempt 1: Full card pattern
    match = CARD_NUMBER_RE.match(name_no_ext)
    if match:
        return _normalize_card_number(match.group(0).strip("- "))

    # Attempt 2: Prefix-AS pattern
    match = PREFIX_AS_RE.match(name_no_ext)
    if match:
        prefix = match.group(0).strip("- ")
        if prefix:
            return prefix

    # Attempt 3: Letter + 2+ digits at start
    match = LETTERS_DIGITS_RE.match(name_no_ext)
    if match:
        return match.group(1)

    # Attempt 4: Digits (min 2) at start of name
    match = OP_NUMBER_IN_FILENAME_RE.match(name_no_ext)
    if match:
        return match.group(1)

    return name_no_ext


def _normalize_card_number(card_no: str) -> str:
    if card_no.upper().startswith("SSQRT"):
        normalized = "SQRT" + card_no[5:]
        logger.debug("Card number normalisation: %s → %s", card_no, normalized)
        return normalized
    return card_no


class HeuristicAnalyzer:
    """Class wrapper for retained helper functions used by services."""

    MAX_COL_SCAN_WIDTH = int(os.environ.get("BURLAK_MAX_COL_SCAN_WIDTH", 200))
    MAX_CACHE_SIZE = 100

    _merged_cell_cache: dict[str, dict[tuple[int, int], tuple[int, int]]] = {}
    _cache_access_order: list[str] = []

    @classmethod
    def _build_merged_cell_map(cls, ws: Any) -> dict[tuple[int, int], tuple[int, int]]:
        """Build map of merged cells from worksheet."""
        cache_key = f"{id(ws)}_{getattr(ws, 'title', '')}"
        if cache_key in cls._merged_cell_cache:
            # Move to end to track LRU
            if cache_key in cls._cache_access_order:
                cls._cache_access_order.remove(cache_key)
            cls._cache_access_order.append(cache_key)
            return cls._merged_cell_cache[cache_key]

        merged_map: dict[tuple[int, int], tuple[int, int]] = {}
        try:
            merged_ranges = getattr(ws, "merged_cells", None)
            if merged_ranges:
                for cr in merged_ranges.ranges:
                    min_col, min_row, max_col, max_row = (
                        cr.min_col,
                        cr.min_row,
                        cr.max_col,
                        cr.max_row,
                    )
                    top_left = (min_row, min_col)
                    for r in range(min_row, max_row + 1):
                        for c in range(min_col, max_col + 1):
                            if (r, c) != top_left:
                                merged_map[(r, c)] = top_left
        except Exception as e:
            logger.warning("Error building merged cells map: %s", e)

        # LRU cache management
        if len(cls._merged_cell_cache) >= cls.MAX_CACHE_SIZE:
            oldest_key = cls._cache_access_order.pop(0)
            cls._merged_cell_cache.pop(oldest_key, None)

        cls._merged_cell_cache[cache_key] = merged_map
        cls._cache_access_order.append(cache_key)
        return merged_map

    @staticmethod
    def get_cell_value(ws: Any, row: int, col: int) -> Any:
        """Read a cell value handling merged cells."""
        import math

        try:
            if hasattr(ws, "cell_value"):
                val = ws.cell_value(row, col)
            else:
                val = ws.cell(row=row, column=col).value

            if val is None and row > 0 and col > 0:
                merged_map = HeuristicAnalyzer._build_merged_cell_map(ws)
                source = merged_map.get((row, col))
                if source is not None:
                    top_row, top_col = source
                    if hasattr(ws, "cell_value"):
                        val = ws.cell_value(top_row, top_col)
                    else:
                        val = ws.cell(row=top_row, column=top_col).value

            if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                return None
            return val
        except Exception as e:
            logger.debug("Error getting cell value at (%d, %d): %s", row, col, e)
            return None

    @staticmethod
    def is_cell_strike(ws: Any, row: int, col: int) -> bool:
        """Check if a cell has strikethrough styling."""
        try:
            if hasattr(ws, "cell_font"):
                font = ws.cell_font(row, col)
                return bool(font and getattr(font, "strike", False))
            else:
                cell = ws.cell(row=row, column=col)
                return bool(cell.font and cell.font.strike)
        except Exception:
            return False

    @staticmethod
    def get_strike_rows(ws: Any, rows: range, cols: list[int]) -> set[int]:
        """Batch-detect rows with strikethrough in any of the given columns.

        Returns a set of row numbers where at least one cell has strikethrough.
        This is much faster than calling is_cell_strike() per cell.
        """
        strike_rows = set()
        for row in rows:
            for col in cols:
                try:
                    if hasattr(ws, "_ws") and hasattr(ws, "_engine"):
                        if ws._engine != "openpyxl":
                            break
                        cell = ws._ws.cell(row=row, column=col)
                    elif hasattr(ws, "cell"):
                        cell = ws.cell(row=row, column=col)
                    else:
                        break
                    if cell is None:
                        continue
                    font = cell.font
                    if font is None:
                        continue
                    strike_val = getattr(font, "strike", None)
                    if strike_val is None:
                        continue
                    is_strike = False
                    if isinstance(strike_val, str):
                        is_strike = strike_val.lower() in (
                            "sngstrike",
                            "dblstrike",
                            "true",
                        )
                    else:
                        is_strike = bool(strike_val)
                    if is_strike:
                        strike_rows.add(row)
                        break  # Already found for this row, no need to check other cols
                except Exception:
                    continue
        return strike_rows

    @staticmethod
    def _get_part_no_keywords() -> list[str]:
        return PART_NO_KEYWORDS

    @staticmethod
    def _is_false_positive_part_no(val: str, keyword: str) -> bool:
        if not val:
            return False
        _COMPOUND_PREFIXES: dict[str, list[str]] = {
            "件号": ["更改", "文件", "变更", "修订", "版本"],
        }
        prefixes = _COMPOUND_PREFIXES.get(keyword, [])
        for prefix in prefixes:
            if prefix in val and keyword in val:
                prefix_pos = val.find(prefix)
                kw_pos = val.find(keyword)
                if prefix_pos >= 0 and kw_pos >= 0 and prefix_pos < kw_pos:
                    return True
        return False

    @staticmethod
    def find_part_table(
        ws: Any,
        start_row: int = 1,
    ) -> tuple[int, int, int, int] | None:
        """Find parts table coordinates in worksheet."""
        max_row = ws.max_row or 200
        max_col = ws.max_column or 200
        scan_width = HeuristicAnalyzer.MAX_COL_SCAN_WIDTH

        for row_idx in range(start_row, max_row + 1):
            row_values: list[str] = []
            for col_idx in range(1, min(max_col + 1, scan_width + 1)):
                v = HeuristicAnalyzer.get_cell_value(ws, row_idx, col_idx)
                row_values.append(str(v).strip().lower() if v is not None else "")

            if not any(row_values):
                continue

            non_empty_count = sum(1 for v in row_values if v)
            if non_empty_count < 2:
                continue

            has_part_no = any(
                not HeuristicAnalyzer._is_false_positive_part_no(v, kw)
                for v in row_values
                for kw in PART_NO_KEYWORDS
                if kw in v
            )
            if not has_part_no:
                continue

            part_no_col: int | None = None
            qty_col: int | None = None
            name_col: int | None = None

            for kw in PART_NO_KEYWORDS:
                if part_no_col is not None:
                    break
                for col_idx, val in enumerate(row_values, 1):
                    if kw in val and len(val) < 50:
                        if HeuristicAnalyzer._is_false_positive_part_no(val, kw):
                            continue
                        part_no_col = col_idx
                        break

            if part_no_col is None:
                continue

            for col_idx, val in enumerate(row_values, 1):
                if qty_col is None and not any(ak in val for ak in QTY_ANTI_KEYWORDS):
                    if any(kw in val for kw in QTY_KEYWORDS):
                        qty_col = col_idx
                if name_col is None and any(kw in val for kw in NAME_KEYWORDS):
                    if not any(ak in val for ak in NAME_ANTI_KEYWORDS):
                        name_col = col_idx

            # Multi-row header scan below
            if qty_col is None or name_col is None:
                for scan_offset in range(1, min(11, max_row - row_idx + 1)):
                    scan_row = row_idx + scan_offset
                    scan_values: list[str] = []
                    for col_idx in range(1, min(max_col + 1, scan_width)):
                        v = HeuristicAnalyzer.get_cell_value(ws, scan_row, col_idx)
                        scan_values.append(
                            str(v).strip().lower() if v is not None else ""
                        )

                    if not any(scan_values):
                        continue

                    has_pn_scan = any(
                        not HeuristicAnalyzer._is_false_positive_part_no(sv, kw)
                        for sv in scan_values
                        for kw in PART_NO_KEYWORDS
                        if kw in sv
                    )
                    if has_pn_scan:
                        break

                    if qty_col is None:
                        for col_idx, val in enumerate(scan_values, 1):
                            if any(kw in val for kw in QTY_KEYWORDS):
                                qty_col = col_idx
                                break
                    if name_col is None:
                        for col_idx, val in enumerate(scan_values, 1):
                            if any(kw in val for kw in NAME_KEYWORDS):
                                name_col = col_idx
                                break

                    if qty_col is not None and name_col is not None:
                        break

            # Row-above header scan
            if qty_col is None or name_col is None:
                for scan_offset in range(1, min(6, row_idx)):
                    scan_row = row_idx - scan_offset
                    scan_values: list[str] = []
                    for col_idx in range(1, min(max_col + 1, scan_width)):
                        v = HeuristicAnalyzer.get_cell_value(ws, scan_row, col_idx)
                        scan_values.append(
                            str(v).strip().lower() if v is not None else ""
                        )

                    if not any(scan_values):
                        continue

                    has_pn_above = any(
                        not HeuristicAnalyzer._is_false_positive_part_no(sv, kw)
                        for sv in scan_values
                        for kw in PART_NO_KEYWORDS
                        if kw in sv
                    )
                    if has_pn_above:
                        continue

                    if qty_col is None:
                        for col_idx, val in enumerate(scan_values, 1):
                            if any(kw in val for kw in QTY_KEYWORDS):
                                qty_col = col_idx
                                break
                    if name_col is None:
                        for col_idx, val in enumerate(scan_values, 1):
                            if any(kw in val for kw in NAME_KEYWORDS):
                                name_col = col_idx
                                break

                    if qty_col is not None and name_col is not None:
                        break

            return (
                row_idx,
                part_no_col,
                qty_col or 0,
                name_col or 0,
            )

        return None

    @staticmethod
    def extract_operation_name(ws: Any, table_header_row: int) -> str:
        """Extract operation name from sheet header."""
        max_col = min(ws.max_column or 20, 10)
        service_kws = [
            "作业指导书",
            "文件编号",
            "工具/夹具",
            "版本",
            "发行时间",
            "关键点",
            "车间",
            "序号",
            "变更记录",
            "物料清单",
            "说明性符号",
            "编制",
            "校对",
            "审核",
            "批准",
            "无",
        ]

        for r in range(1, min(table_header_row, 15)):
            for c in range(1, max_col + 1):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is None:
                    continue
                text = str(v).strip()
                if any(kw in text for kw in service_kws):
                    continue
                if len(text) > 3 and bool(CJK_RE.search(text)):
                    if "作业要素" in text:
                        for check_c in range(c + 1, min(c + 3, max_col + 1)):
                            nv = HeuristicAnalyzer.get_cell_value(ws, r, check_c)
                            if (
                                nv
                                and len(str(nv).strip()) > 1
                                and "作业要素" not in str(nv)
                            ):
                                return str(nv).strip()
                    else:
                        return text
        return ""
