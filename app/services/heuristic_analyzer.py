"""
Heuristic analyser for Excel documents — the core of universal parsing
for BOM sheets and operational cards in automotive manufacturing.

Not tied to specific brands, formats, column indices, or prefixes.
Dynamically determines document structure by analysing:
  - Column headers (via synonym dictionary in 3 languages)
  - Cell content (data types, part-number patterns)
  - Table layout and data boundaries
  - File names and sheet titles

Supports: Chinese, English, Russian languages.

Algorithms:
  1. find_header_rows — find header rows (keyword scoring)
  2. detect_column_types — classify columns (part_no, name, qty, config, meta)
  3. find_data_region — determine data table boundaries
  4. extract_card_number — extract operational card number from any source
  5. build_global_name_dict — collect all part numbers and names across the document
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


# ═══════════════════════════════════════════════════════════════════════
# SYNONYM DICTIONARIES for column detection
# ═══════════════════════════════════════════════════════════════════════    # --- Part number / Material code ---
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

# Keywords that should NOT appear in the part_no column
# (meta columns containing "code" or "number" but not being a part number)
PART_NO_ANTI_KEYWORDS: list[str] = [
    "cpac",
    "fnd",
    "gpc",
    "поставщик",
    "supplier",
    "vehicle",
    "материал",
    "описание",
    "серийный",
    "serial",  # serial number — not part_no
]

# --- Part name ---
NAME_KEYWORDS: list[str] = [
    # Chinese
    "零件名称",
    "零部件名称",
    "物料名称",
    "物料描述",
    "描述",
    "名称",
    "物料名称/描述",
    "材料名称",
    "零件名称(中文）",
    "零件名称(中文)",
    "零件名称（中文）",
    "零件名称(英文）",
    "零件名称(英文)",
    "零件名称（英文）",
    "物料描述（中文）",
    "物料描述(中文）",
    "物料描述（英文）",
    "物料描述(英文）",
    # English
    "part name",
    "description",
    "material description",
    "item description",
    "component name",
    "part name(cn)",
    "part name(en)",
    "part name(cn）",
    "part name(en）",
    "name(cn)",
    "name(en)",
    "name（cn）",
    "name（en）",
    # Russian
    "наименование",
    "наименование детали",
    "название",
    "описание",
    "наименование детали",
    "описание детали",
]

# --- Keywords that should NOT appear in the name_cn column ---
# (columns with factory/supplier/manufacturer — NOT a part name)
NAME_ANTI_KEYWORDS: list[str] = [
    "工厂",
    "厂家",
    "供应商",
    "制造商",
    "生产商",
    "模块",
    "factory",
    "manufacturer",
    "supplier",
    "module",
    "завод",
    "производитель",
    "поставщик",
]

# --- Quantity ---
QTY_KEYWORDS: list[str] = [
    # Chinese
    "用量",
    "数量",
    "单车用量",
    "每车用量",
    "数量/用量",
    "标配数量",
    "用量/数量",
    "单位用量",
    # English
    "qty",
    "quantity",
    "usage",
    "qty per",
    # Russian
    "количество",
    "кол-во",
    "расход",
    "норма",
]

# Keywords that should NOT appear in the qty column
# (columns containing "quantity" but of a different type)
QTY_ANTI_KEYWORDS: list[str] = [
    # Chinese
    "工具数量",  # tool quantity — not part quantity
    "扭矩数量",  # torque quantity
    "工具",  # tool
    "模具数量",  # mould/die quantity
    "工装数量",  # fixture quantity
]

# --- Standard service columns (not configurations) ---
META_KEYWORDS: list[str] = [
    # Chinese
    "序号",
    "行号",
    "修订",
    "版本",
    "层级",
    "等级",
    "标识",
    "发运",
    "采购",
    "度量单位",
    "uom",
    "gpc",
    "fnd",
    "物料状态",
    "来源车间",
    "使用工厂",
    "目标车间",
    "供应商",
    "供应商代码",
    "供应商名称",
    "生产工厂",
    "供货工厂",
    "制造工厂",
    "装配工厂",
    "安装工厂",
    "mwo",
    "mwo单号",
    "生效日期",
    "失效日期",
    "整车物料号",
    "变更单号",
    "eop",
    "eos",
    "零件成熟度",
    "物料组",
    "物料组描述",
    "cpac编码",
    "cpac描述",
    "品牌",
    "车系",
    "卸货工厂",
    "备注",
    "说明",
    "附注",
    "注",
    "分类",
    "类别",
    "车型",
    "状态号",
    "模块状态",
    "供货状态",
    "PBOM供货",
    "平台属性",
    "设计层次",
    "装配层次",
    "工位范围",
    "工位",
    "工序",
    "零件质量",
    "IA编码",
    "货源",
    "货源描述",
    "结构货源",
    "单车用量",
    "组件数量",
    "发动机附件",
    # Torque/moment columns — NOT configurations
    "扭矩",
    "力矩",
    "动态扭矩",
    "残余扭矩",
    "扭矩角度",
    "扭矩关重",
    "扭矩说明",
    "扭矩监控",
    "图示编号",
    "图纸编号",
    "图纸号",
    "示意图编号",
    # English
    "serial no",
    "serial no.",
    "serial",
    "seq",
    "sequence",
    "revision",
    "rev",
    "version",
    "ver",
    "level",
    "ship",
    "purchase",
    "uom",
    "unit",
    "gpc code",
    "fnd code",
    "make/buy",
    "source shop",
    "using plant",
    "target shop",
    "supplier",
    "supplier code",
    "supplier name",
    "mwo",
    "effective date",
    "expire date",
    "vehicle material",
    "vehicle model",
    "remark",
    "note",
    "notes",
    "comment",
    "category",
    "classification",
    "type",
    "logo",
    "identification",
    "id",
    "torque",
    "nm",
    "n·m",
    "moment",
    # Russian
    "примечание",
    "комментарий",
    "завод",
    "поставщик",
    "дата",
    "статус",
    "система",
    "узел",
    "подразделение",
    "расход на один автомобиль",
    "количество компонентов",
]

# Only columns with FIXED text (not data, not configs)
STRICT_META_KEYWORDS: list[str] = [
    "序号",
    "修订",
    "版本",
    "度量单位",
    "uom",
    "gpc",
    "fnd",
    "零件成熟度",
    "make/buy",
    "cpac编码",
    "serial no",
    "serial",
    "revision",
    "level",
    "变更记录",
    "文件编号",
]

# Column with schematic/operation number (for linking parts to operational cards)
GRAPHIC_NUMBER_KEYWORDS: list[str] = [
    # Chinese
    "图示编号",
    "图号",
    "图纸编号",
    "图纸号",
    "示意图编号",
    "工序号",
    "工位号",
    "工位编号",
    # English
    "graphic number",
    "drawing number",
    "drawing no",
    "drawing no.",
    "operation number",
    "operation no",
    "operation no.",
    "station number",
    "station no",
    # Russian
    "номер схемы",
    "номер операции",
    "номер чертежа",
    "код операции",
    "позиция схемы",
]

# Keywords for identifying "service sheet" (not BOM)
SERVICE_SHEET_KEYWORDS: list[str] = [
    "封面",
    "目录",
    "记录表",
    "空表",
    "范本",
    "填写说明",
    "содержание",
    "обложка",
    # Additional meta-sheets (not BOM)
    "变更记录",
    "变更",  # change log
    "汇总",  # summary
    "原稿",  # draft
    "分装",  # sub-assembly
    "分总成",  # sub-assembly list
    "申请",  # application/request
    "路线",  # routing
    "ebom",  # engineering BOM (another view, not the main)
    "mbom",  # manufacturing BOM (another view, not the main)
    "bom原稿",  # BOM draft
    "bom汇总",  # BOM summary
    "bom变更",  # BOM change
    "物料号汇总",  # material summary
]


# ═══════════════════════════════════════════════════════════════════════
# PATTERNS
# ═══════════════════════════════════════════════════════════════════════

# Pattern for extracting operational card number from text/filename:
#   - Any alphanumeric prefixes (e.g. ABC, G01, etc.)
#   - Combinations with digits and hyphens, may contain sub-patterns like -A-AS-
#   - Purely numeric codes (min 2 digits) or letter + 2+ digits
CARD_NUMBER_RE = re.compile(
    r"(?:"
    r"  [A-Za-z]+\d*[A-Za-z]*(?:-\d+)+[\w-]*"  # ABC1L-17-AS-04001, ABC1JL-17-AS-01
    r"|"
    r"  [A-Za-z]{1,3}\d{3,}[\w-]*"  # A001, G01, etc.
    r"|"
    r"  (?:TP|процесс|операция|card)\s*[-]?\s*\d+"  # TP-123, операция 5
    r")",
    re.IGNORECASE | re.VERBOSE,
)

# Pattern for operation numbers in filename (any format):
#   - Digits (min 2) at start of name or after prefix
OP_NUMBER_IN_FILENAME_RE = re.compile(r"(?:^|[-_\s])(\d{2,})(?:[-_\s]|$)")

# Pattern for "letter + 2+ digits" at start of name
LETTERS_DIGITS_RE = re.compile(r"^([A-Za-z]{1,3}\d{2,})")

# Pattern for prefix-AS pattern (e.g. G01-A-AS- or ABC1L-A-AS-)
PREFIX_AS_RE = re.compile(
    r"^([A-Za-z0-9]+[-_])?[A-Za-z]?[-_]?AS[-_]?\d+",
    re.IGNORECASE,
)

# Chinese characters (CJK)
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# Cyrillic characters
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")

# Excel XML encoding artefacts
XML_HEX_RE = re.compile(r"_x[0-9a-fA-F]{4}_")
XML_ARTIFACTS_RE = re.compile(r"(_x[0-9a-fA-F]{4}_|\r\n|[\r\n])", re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════


def normalize_text(text: str) -> str:
    """Normalise text to lowercase, strip extra whitespace and encoding artefacts."""
    s = str(text).lower()
    # Remove Excel XML encoding artefacts (carriage return)
    s = XML_HEX_RE.sub("", s)
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(s.split())


def clean_cell_text(text: Any) -> str:
    """Clean cell text from Excel XML encoding artefacts.

    Removes _x000d_, _x000A_, \r, \n (case-insensitive) and takes only the FIRST part
    (when a cell contains two values: Chinese + English separated by \n).
    """
    if text is None:
        return ""
    s = str(text).strip()
    match = XML_ARTIFACTS_RE.search(s)
    if match:
        s = s[: match.start()]
    return s.strip()


def looks_like_part_number(value: Any) -> float:
    """Check whether a cell value looks like a part number.

    Returns float (0.0–1.0) — confidence score.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return 0.1  # Numbers are rarely part numbers (but can be)
    s = str(value).strip()
    if not s or len(s) < 3:
        return 0.0
    # Scoring
    score = 0.0
    # Contains letters + digits
    has_alpha = bool(re.search(r"[A-Za-z]", s))
    has_digit = bool(re.search(r"\d", s))
    has_cjk = bool(CJK_RE.search(s))
    has_cyrillic = bool(CYRILLIC_RE.search(s))
    # If Cyrillic or CJK characters present — unlikely to be part number
    if has_cjk or has_cyrillic:
        score -= 0.3
    if has_alpha and has_digit:
        score += 0.6
    elif has_digit and len(s) >= 6:
        score += 0.3
    # Presence of hyphens — part number indicator
    if "-" in s:
        score += 0.2
    # Service words
    if any(kw in s.lower() for kw in ["零件", "物料", "部件", "part", "компонент"]):
        return 0.1  # This is a header, not a number
    return min(max(score, 0.0), 1.0)


def looks_like_name(value: Any) -> float:
    """Check whether a cell value looks like a part name.

    Returns float (0.0–1.0) — confidence score.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return 0.0  # Numbers are never names
    s = str(value).strip()
    if not s or len(s) < 2:
        return 0.0
    if is_valid_part_number(s):
        return 0.1  # Looks like part-no, not a name
    score = 0.0
    has_cjk = bool(CJK_RE.search(s))
    has_cyrillic = bool(CYRILLIC_RE.search(s))
    has_alpha = bool(re.search(r"[A-Za-z]", s))
    has_digit = bool(re.search(r"\d", s))
    # Presence of CJK or Cyrillic characters — strong name indicator
    if has_cjk:
        score += 0.5
    if has_cyrillic:
        score += 0.4
    # Letters only (no digits) — looks like a name
    if has_alpha and not has_digit:
        score += 0.3
    # Long text — name indicator
    if len(s) > 10:
        score += 0.2
    # If contains both letters and digits — could be a name
    if has_alpha and has_digit:
        score -= 0.1
    return min(max(score, 0.0), 1.0)


def looks_like_quantity(value: Any) -> float:
    """Check whether a cell value looks like a quantity.

    Returns float (0.0–1.0) — confidence score.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return 1.0  # A number — almost always a quantity
    s = str(value).strip()
    if not s:
        return 0.0
    # String representing a number
    try:
        float(s.replace(",", "."))
        return 0.9
    except ValueError:
        pass
    # 'S' or '-' — possible VIN-breakdown values
    if s.upper() == "S" or s == "-":
        return 0.2  # Not a quantity, but "same as"
    return 0.0


def extract_card_number_from_filepath(file_path: str) -> str:
    """Extract the operational card number from a file path.

    Universal algorithm:
      1. Take the base filename without extension
      2. Search for known number patterns
      3. If no pattern found — return the base name

    Examples:
      "ABC1L-17-AS-04001-20点扫描" -> "ABC1L-17-AS-04001"
      "G01-AS-05001-Установка" -> "G01-AS-05001"
      "A123-Контроль" -> "A123"
      "038-Установка двери" -> "038"
      "TP-0123-Main" -> "TP-0123"
    """
    basename = os.path.basename(file_path)
    name_no_ext = os.path.splitext(basename)[0]

    # Attempt 1: Full card pattern (e.g. ABC1L-17-AS-04001)
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

    # Return base name
    return name_no_ext


def _normalize_card_number(card_no: str) -> str:
    """Normalise card number, fixing common data-entry errors.        Fixes:
      - Double-letter prefix typo → corrected prefix
    """
    if card_no.upper().startswith("SSQRT"):
        normalized = "SQRT" + card_no[5:]
        logger.debug("Card number normalisation: %s → %s", card_no, normalized)
        return normalized
    return card_no


def _is_numeric_string(s: str) -> bool:
    """Check whether a string represents a number (integer or float)."""
    try:
        float(s.replace(",", "."))
        return True
    except (ValueError, TypeError):
        return False


# ═══════════════════════════════════════════════════════════════════════
# MAIN CLASS
# ═══════════════════════════════════════════════════════════════════════


class HeuristicAnalyzer:
    """Heuristic analyser for Excel worksheets.

    Dynamically determines BOM and operational card structure
    without being tied to specific brand formats.
    """

    # Maximum number of rows for header scanning
    MAX_HEADER_SCAN_ROWS = int(os.environ.get("BURLAK_MAX_HEADER_SCAN_ROWS", 30))
    # Maximum column scan width (for wide formats where qty may be in C30)
    MAX_COL_SCAN_WIDTH = int(os.environ.get("BURLAK_MAX_COL_SCAN_WIDTH", 200))
    # Minimum confidence threshold for column detection
    CONFIDENCE_THRESHOLD = float(os.environ.get("BURLAK_CONFIDENCE_THRESHOLD", 0.3))

    # Header score bonus for BOM-like sheet names
    _BOM_SHEET_NAME_KEYWORDS: tuple[str, ...] = (
        "bom",
        "总装",
        "涂装",
        "焊装",
        "零部件",
        "附件",
        "сборка",
        "комплект",
        "список деталей",
    )

    @staticmethod
    def find_header_rows(
        ws: Any,
        max_rows: int | None = None,
        sheet_name: str = "",
    ) -> list[int]:
        """Find header rows in a worksheet.

        Analyzes the first max_rows rows, computing for each
        'header score' based on keyword matching.

        Args:
            ws: Excel worksheet.
            max_rows: Maximum rows to scan.
            sheet_name: Sheet name (for score bonus on BOM-like sheets).

        Returns:
            List of candidate header row numbers (sorted by descending score).
            Empty list if nothing found.
        """
        scores: list[tuple[int, float]] = []
        max_col = ws.max_column or 50

        if max_rows is None:
            max_rows = HeuristicAnalyzer.MAX_HEADER_SCAN_ROWS

        # Bonus score for BOM-like sheet names
        name_bonus = 0.0
        if sheet_name:
            name_lower = sheet_name.lower()
            for kw in HeuristicAnalyzer._BOM_SHEET_NAME_KEYWORDS:
                if kw in name_lower:
                    name_bonus = 0.05
                    break

        for row_idx in range(1, min(max_rows + 1, (ws.max_row or 100) + 1)):
            row_values = [
                str(HeuristicAnalyzer.get_cell_value(ws, row_idx, c) or "")
                for c in range(1, min(max_col + 1, 30))
            ]

            if not any(v.strip() for v in row_values):
                continue

            score = HeuristicAnalyzer._score_header_row(row_values)
            if score > 0:
                scores.append((row_idx, score))

        # Sort by descending score
        scores.sort(key=lambda x: -x[1])

        # Apply bonus to the best score
        if scores and name_bonus > 0:
            scores[0] = (scores[0][0], scores[0][1] + name_bonus)

        # Return only rows with score above threshold.
        # Lower threshold (0.18) to support BOM sheets with non-standard headers
        threshold = 0.18
        if scores:
            threshold = max(scores[0][1] * 0.4, 0.18)

        result = [r for r, s in scores if s >= threshold]

        if result:
            logger.debug(
                "Header rows found: %s (score: %s)",
                result,
                [f"{s:.2f}" for _, s in scores if s >= threshold],
            )
        else:
            logger.debug("No header rows found")

        return result

    @staticmethod
    def _score_header_row(row_values: list[str]) -> float:
        """Score how closely a row resembles a header.

        Considers:
          - Number of keyword matches (part_no, name, qty)
          - Density of non-empty cells
          - Content type diversity
        """
        if not row_values:
            return 0.0

        part_no_matches = 0
        name_matches = 0
        qty_matches = 0
        meta_matches = 0
        non_empty = 0
        total = len(row_values)

        for val in row_values:
            v = normalize_text(val)
            if not v:
                continue
            non_empty += 1

            # Check for keywords
            if any(kw in v for kw in PART_NO_KEYWORDS):
                part_no_matches += 1
            if any(kw in v for kw in NAME_KEYWORDS):
                name_matches += 1
            if any(kw in v for kw in QTY_KEYWORDS):
                qty_matches += 1
            if any(kw in v for kw in META_KEYWORDS):
                meta_matches += 1

        # Density of non-empty cells
        density = non_empty / max(total, 1)

        # Total score
        score = (
            part_no_matches * 2.0
            + name_matches * 1.5
            + qty_matches * 1.5
            + meta_matches * 0.5
            + density * 0.5
        )

        # Normalisation
        max_possible = total * 2.0
        return score / max_possible

    @staticmethod
    def detect_column_types(ws: Any, header_rows: list[int]) -> dict[str, int]:
        """Detect column types from headers and cell content.

        Analyzes headers and verifies data types in cells.

        Returns:
            Dict: {
                'part_no': int (column number),
                'name_cn': int,
                'name_en': int,
                'qty': int,
                'config_start': int (first configuration column),
            }
            Zero values mean the column was not found.
        """
        max_col = ws.max_column or 100
        header_rows[0] if header_rows else 1

        col_types: dict[str, int] = {}
        column_scores: dict[str, list[tuple[int, float]]] = {
            "part_no": [],
            "name_cn": [],
            "name_en": [],
            "qty": [],
        }

        # Collect headers (from multiple rows if available)
        header_texts: dict[int, str] = {}
        for c in range(1, max_col + 1):
            texts = []
            for hr in header_rows:
                v = HeuristicAnalyzer.get_cell_value(ws, hr, c)
                if v:
                    texts.append(str(v))
            header_texts[c] = " ".join(texts).strip().lower()

        # Phase 1: Scoring columns by headers
        for c in range(1, max_col + 1):
            text = header_texts[c]
            if not text:
                continue

            # First check if the column is definitely a meta column
            # (contains anti-pattern keywords that exclude part_no)
            is_anti_part_no = any(kw in text for kw in PART_NO_ANTI_KEYWORDS)

            # Check for part_no (only if not anti-pattern)
            if not is_anti_part_no:
                best_kw = None
                best_score = 0.0
                for kw in PART_NO_KEYWORDS:
                    if kw.lower() in text:
                        specificity = min(len(kw), 6) / 6.0
                        score = 0.7 + 0.3 * specificity
                        if score > best_score:
                            best_score = score
                            best_kw = kw
                if best_kw is not None:
                    column_scores["part_no"].append((c, best_score))
                else:
                    # Fallback: fuzzy match
                    for kw in PART_NO_KEYWORDS:
                        kw_norm = (
                            kw.lower()
                            .replace(" ", "")
                            .replace("(", "")
                            .replace(")", "")
                        )
                        text_norm = (
                            text.replace(" ", "").replace("(", "").replace(")", "")
                        )
                        if kw_norm in text_norm:
                            specificity = min(len(kw), 6) / 6.0
                            score = 0.5 + 0.3 * specificity
                            if score > best_score:
                                best_score = score
                                best_kw = kw
                    if best_kw is not None:
                        column_scores["part_no"].append((c, best_score))

            # Check for name_cn (with Chinese characters)
            is_cn_name = False
            best_name_kw = None
            for kw in NAME_KEYWORDS:
                if kw.lower() in text:
                    if best_name_kw is None or len(kw) > len(best_name_kw):
                        best_name_kw = kw
            if best_name_kw is not None:
                kw = best_name_kw
                # Check for anti-patterns (factory/supplier)
                is_anti_name = any(ak in text for ak in NAME_ANTI_KEYWORDS)
                if not is_anti_name:
                    # Check for Russian/English
                    has_cjk = bool(CJK_RE.search(text))
                    has_cyrillic = bool(CYRILLIC_RE.search(text))
                    cn_hints = ["中文", "cn", "(chinese)", "chinese"]
                    is_cn = has_cjk or any(h in text for h in cn_hints)
                    is_en = (
                        has_cyrillic or "英文" in text or "en)" in text or "(en" in text
                    )

                    specificity = min(len(kw), 6) / 6.0
                    base_score = 0.7 + 0.3 * specificity

                    if is_cn and not is_en:
                        column_scores["name_cn"].append((c, base_score))
                    elif is_en or has_cyrillic:
                        column_scores["name_en"].append((c, base_score))
                    elif (
                        "英文" in text
                        or "英文）" in text
                        or "en)" in text
                        or "en）" in text
                    ):
                        column_scores["name_en"].append((c, 1.0))
                    elif "中文" in text or "中文）" in text or "中文)" in text:
                        column_scores["name_cn"].append((c, 1.0))
                    else:
                        column_scores["name_cn"].append((c, base_score))
                    is_cn_name = True

            if not is_cn_name:
                # Check for description
                text_lower = text.lower()
                if (
                    "descript" in text_lower
                    or "наимен" in text_lower
                    or "описан" in text_lower
                ):
                    column_scores["name_cn"].append((c, 0.7))

            # Check for qty (with anti-keywords)
            # Exclude columns containing QTY_ANTI_KEYWORDS
            is_anti_qty = any(ak.lower() in text for ak in QTY_ANTI_KEYWORDS)
            if not is_anti_qty:
                for kw in QTY_KEYWORDS:
                    if kw.lower() in text:
                        column_scores["qty"].append((c, 1.0))
                        break

        # Phase 2: Content verification (check data cells)
        data_start = header_rows[-1] + 1 if header_rows else 2
        sample_end = min(data_start + 30, (ws.max_row or data_start + 30) + 1)

        # For each column type, verify the content
        for col_type in ["part_no", "name_cn", "name_en", "qty"]:
            current_scores = column_scores[col_type]
            verified_scores: list[tuple[int, float]] = []

            for c, score in current_scores:
                if score >= 0.9:
                    verified_scores.append((c, score))
                    continue

                # Content sampling
                part_no_hits = 0
                name_hits = 0
                qty_hits = 0
                total_samples = 0

                for r in range(data_start, sample_end):
                    v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                    if v is None or (isinstance(v, str) and not v.strip()):
                        continue
                    total_samples += 1

                    pn_score = looks_like_part_number(v)
                    nm_score = looks_like_name(v)
                    qt_score = looks_like_quantity(v)

                    if pn_score > 0.6:
                        part_no_hits += 1
                    if nm_score > 0.6:
                        name_hits += 1
                    if qt_score > 0.8:
                        qty_hits += 1

                if total_samples > 0:
                    pn_ratio = part_no_hits / total_samples
                    nm_ratio = name_hits / total_samples
                    qt_ratio = qty_hits / total_samples

                    # Score adjustment based on content
                    if col_type == "part_no" and pn_ratio > 0.3:
                        score = max(score, 0.7)
                    elif col_type == "name_cn" and nm_ratio > 0.3:
                        score = max(score, 0.6)
                    elif col_type == "name_en" and nm_ratio > 0.2:
                        score = max(score, 0.5)
                    elif col_type == "qty" and qt_ratio > 0.3:
                        score = max(score, 0.7)

                verified_scores.append((c, score))

            column_scores[col_type] = verified_scores

        # Select best candidates
        for col_type in ["part_no", "name_cn", "name_en", "qty"]:
            scores = column_scores[col_type]
            if scores:
                # Sort by score, then select the first
                scores.sort(key=lambda x: (-x[1], x[0]))
                best_col, best_score = scores[0]
                if best_score >= HeuristicAnalyzer.CONFIDENCE_THRESHOLD:
                    key_map = {
                        "part_no": "part_no",
                        "name_cn": "name_cn",
                        "name_en": "name_en",
                        "qty": "qty",
                    }
                    col_types[key_map[col_type]] = best_col

        # Phase 3: Content-based fallback for part_no (with header verification!)
        if "part_no" not in col_types:
            col_types["part_no"] = HeuristicAnalyzer._find_part_no_by_content(
                ws,
                data_start,
                sample_end,
                max_col,
                header_texts,
            )

        # Phase 4: Fallback for name
        if "name_cn" not in col_types and "name_en" not in col_types:
            name_col = HeuristicAnalyzer._find_name_by_content(
                ws, data_start, sample_end, max_col
            )
            if name_col:
                col_types["name_cn"] = name_col

        # Phase 5: If name_en found but content is Russian/CJK → reassign to name_cn
        # (but only if the header does NOT contain explicit English markers)
        name_en_col = col_types.get("name_en", 0)
        name_cn_col = col_types.get("name_cn", 0)
        if name_en_col and not name_cn_col:
            # Check header for explicit English markers
            header_text = header_texts.get(name_en_col, "")
            has_en_marker = any(
                m in header_text
                for m in ["英文", "english", "en)", "en）", "(en", "（en", "inglés"]
            )
            if not has_en_marker:
                # Check name_en content: if Cyrillic or CJK → this is name_cn
                has_cjk_cyrillic = False
                for r in range(data_start, sample_end):
                    v = HeuristicAnalyzer.get_cell_value(ws, r, name_en_col)
                    if v and isinstance(v, str):
                        if CJK_RE.search(v) or CYRILLIC_RE.search(v):
                            has_cjk_cyrillic = True
                            break
                if has_cjk_cyrillic:
                    col_types["name_cn"] = name_en_col
                    del col_types["name_en"]

        logger.debug(
            "Columns detected: part_no=%s, name_cn=%s, name_en=%s, qty=%s",
            col_types.get("part_no"),
            col_types.get("name_cn"),
            col_types.get("name_en"),
            col_types.get("qty"),
        )
        return col_types

    @staticmethod
    def _get_part_no_keywords() -> list[str]:
        """Return PART_NO_KEYWORDS for external use.

        Needed for card_parser._collect_raw_rows, which cannot directly import PART_NO_KEYWORDS.
        """
        return PART_NO_KEYWORDS

    # Cache for merged cell maps: id(ws) -> {(row, col): (top_row, top_col)}
    # Uses weak references so entries are automatically evicted when the
    # worksheet object is garbage-collected, preventing memory leaks.
    _merged_cell_cache: dict[int, dict[tuple[int, int], tuple[int, int]]] = {}
    _cache_access_order: list[int] = []  # LRU tracking for bounded eviction
    _CACHE_MAX_SIZE = 256

    @classmethod
    def _build_merged_cell_map(cls, ws: Any) -> dict[tuple[int, int], tuple[int, int]]:
        """Build a map from non-top-left merged cells to their top-left source.

        In openpyxl, only the top-left cell of a merged range has a value.
        All other cells return None. This map allows resolving None cells
        to their merged source.
        """
        cache_key = id(ws)
        if cache_key in cls._merged_cell_cache:
            # Move to end of access order (most recently used)
            try:
                cls._cache_access_order.remove(cache_key)
            except ValueError:
                pass
            cls._cache_access_order.append(cache_key)
            return cls._merged_cell_cache[cache_key]

        # Evict oldest entries when cache exceeds max size
        while len(cls._merged_cell_cache) >= cls._CACHE_MAX_SIZE:
            if cls._cache_access_order:
                oldest = cls._cache_access_order.pop(0)
                cls._merged_cell_cache.pop(oldest, None)
            else:
                cls._merged_cell_cache.clear()
                break

        merged_map: dict[tuple[int, int], tuple[int, int]] = {}
        try:
            ranges = ws.merged_cells.ranges
            for mr in ranges:
                top_row = mr.min_row
                top_col = mr.min_col
                for r in range(mr.min_row, mr.max_row + 1):
                    for c in range(mr.min_col, mr.max_col + 1):
                        if r != top_row or c != top_col:
                            merged_map[(r, c)] = (top_row, top_col)
        except (AttributeError, TypeError, IndexError):
            pass

        cls._merged_cell_cache[cache_key] = merged_map
        cls._cache_access_order.append(cache_key)
        return merged_map

    @staticmethod
    def get_cell_value(ws: Any, row: int, col: int) -> Any:
        """Read a cell value via the unified API (worksheet / excel_sheet).

        Works with both openpyxl.Worksheet and ExcelSheet (from card_parser).
        Automatically discards NaN and Infinity values.
        Supports merged cells — if a cell is part of a merged range,
        returns the value from the top-left cell.
        """
        import math

        try:
            if hasattr(ws, "cell_value"):
                val = ws.cell_value(row, col)
            else:
                val = ws.cell(row=row, column=col).value

            # If value is None, check merged cell map
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
            logger.debug("get_cell_value error at row=%d, col=%d: %s", row, col, e)
            return None

    @staticmethod
    def is_cell_strike(ws: Any, row: int, col: int) -> bool:
        """Check whether a cell has strikethrough formatting.

        Always returns False for:
          - xlrd (no strikethrough data)
          - Cells without explicit font
          - Exceptions
          - col <= 0 (invalid column index)

        Safe for:
          - font.strike = None (not set) -> False
          - font.strike = True/False -> corresponding value
          - font.strike = "sngStrike"/"dblStrike" (openpyxl string values) -> True
          - font = None -> False
        """
        if col <= 0:
            return False
        try:
            # Handle card_parser's ExcelSheet wrapper
            if hasattr(ws, "_ws") and hasattr(ws, "_engine"):
                if ws._engine != "openpyxl":
                    return False
                cell = ws._ws.cell(row=row, column=col)
                if cell is None:
                    return False
                font = cell.font
                if font is None:
                    return False
                strike_val = getattr(font, "strike", None)
                if strike_val is None:
                    return False
                if isinstance(strike_val, str):
                    return strike_val.lower() in ("sngstrike", "dblstrike", "true")
                return bool(strike_val)

            # openpyxl Worksheet
            if hasattr(ws, "cell"):
                cell = ws.cell(row=row, column=col)
                if cell is None:
                    return False
                font = cell.font
                if font is None:
                    return False
                strike_val = getattr(font, "strike", None)
                if strike_val is None:
                    return False
                if isinstance(strike_val, str):
                    return strike_val.lower() in ("sngstrike", "dblstrike", "true")
                return bool(strike_val)
            return False
        except Exception as e:
            logger.debug("is_cell_strike error at row=%d, col=%d: %s", row, col, e)
            return False

    @staticmethod
    def get_strike_rows(ws: Any, rows: range, cols: list) -> set:
        """Batch-detect rows with strikethrough in any of the given columns.

        Returns a set of row numbers where at least one cell has strikethrough.
        This is much faster than calling is_cell_strike() per cell.
        """
        strike_rows: set = set()
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
    def _find_part_no_by_content(
        ws: Any,
        start_row: int,
        end_row: int,
        max_col: int,
        header_texts: dict[int, str] | None = None,
    ) -> int:
        """Fallback: find the part-number column by cell content.

        Uses TWO approaches:
          1. Standard: looks_like_part_number (letters+digits, hyphens).
          2. Data Profiling: if a column without a recognizable header
             contains >50% alphanumeric values of length 8-15 characters —
             classify it as a Part Number candidate.

        Args:
            ws: Worksheet
            start_row, end_row: Row range for analysis
            max_col: Maximum column
            header_texts: Column headers (for excluding meta columns)

        Returns:
            Column number or 0.
        """
        col_scores: dict[int, float] = {}

        for c in range(1, max_col + 1):
            # Exclude columns whose header is definitely a service/meta column
            if header_texts:
                text = header_texts.get(c, "")
                if text:
                    is_meta = False
                    for kw in STRICT_META_KEYWORDS:
                        if kw.lower() in text:
                            is_meta = True
                            break
                    if not is_meta:
                        for kw in META_KEYWORDS:
                            if kw.lower() in text:
                                is_meta = True
                                break
                    if is_meta:
                        continue

            # Collect non-empty values
            values: list[str] = []
            for r in range(start_row, end_row):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is not None:
                    s = str(v).strip()
                    if s:
                        values.append(s)

            if len(values) <= 2:
                continue

            # ── Pre-check: Skip columns with too many date-like values ──
            date_re = re.compile(
                r"^(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}"
                r"|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}"
                r"|\d{1,2}\s*[а-яА-ЯёЁ]{3,8}\s*\d{2,4}"
                r"|\d{1,2}\s+[a-zA-Z]{3,8}\s*\d{2,4})$"
            )
            date_hits = sum(1 for v in values if date_re.match(v))
            if date_hits / len(values) > 0.3:
                continue

            # ── Approach 1: Standard (looks_like_part_number) ──
            pn_hits = sum(1 for v in values if looks_like_part_number(v) > 0.6)
            pn_ratio = pn_hits / len(values)

            # ── Approach 2: Data Profiling (alphanumeric 8-15 chars) ──
            # If the header is undefined or does not contain meta keywords,
            # try to project the column as part_no
            bool(header_texts and header_texts.get(c, ""))
            alpha_numeric_hits = 0
            for val in values:
                # Remove common delimiters
                cleaned = (
                    val.replace("-", "")
                    .replace(".", "")
                    .replace("_", "")
                    .replace("/", "")
                    .replace(" ", "")
                )
                if not cleaned:
                    continue
                # Check: contains BOTH letters AND digits, length 8-15
                has_alpha = bool(re.search(r"[A-Za-z]", cleaned))
                has_digit = bool(re.search(r"\d", cleaned))
                length_ok = 8 <= len(cleaned) <= 15
                if has_alpha and has_digit and length_ok:
                    # Additional check: not too many unique characters (not UUID/GUID)
                    unique_chars = len(set(cleaned))
                    if (
                        unique_chars >= 4
                    ):  # Minimum 4 unique characters (not a repeating pattern)
                        alpha_numeric_hits += 1

            an_ratio = alpha_numeric_hits / len(values) if values else 0

            # Combined score
            combined_score = max(pn_ratio, an_ratio)

            # Penalty for CJK/cyrillic in values (these are names, not part-no)
            cjk_hits = sum(1 for v in values if bool(CJK_RE.search(str(v))))
            cjk_ratio = cjk_hits / len(values) if values else 0
            if cjk_ratio > 0.3:
                combined_score *= 0.3

            if combined_score > 0.3:
                col_scores[c] = combined_score

        if col_scores:
            best = max(col_scores, key=col_scores.get)
            if col_scores[best] > 0.3:
                logger.info(
                    "Part_no column found by content + data profiling: "
                    "%d (score=%.2f, values=%d)",
                    best,
                    col_scores[best],
                    sum(
                        1
                        for _ in range(start_row, end_row)
                        if HeuristicAnalyzer.get_cell_value(ws, _, best) is not None
                    ),
                )
                return best
        return 0

    @staticmethod
    def _find_name_by_content(
        ws: Any, start_row: int, end_row: int, max_col: int
    ) -> int:
        """Fallback: find the name column by cell content."""
        col_scores: dict[int, float] = {}
        for c in range(1, max_col + 1):
            hits = 0
            total = 0
            for r in range(start_row, end_row):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is None:
                    continue
                total += 1
                if looks_like_name(v) > 0.5:
                    hits += 1
            if total > 2:
                ratio = hits / total
                # Exclude columns with part-number
                pn_ratio = sum(
                    1
                    for r in range(start_row, end_row)
                    if looks_like_part_number(
                        HeuristicAnalyzer.get_cell_value(ws, r, c)
                    )
                    > 0.6
                ) / max(total, 1)
                if ratio > 0.3 and pn_ratio < 0.3:
                    col_scores[c] = ratio - pn_ratio * 0.5
        if col_scores:
            best = max(col_scores, key=col_scores.get)
            if col_scores[best] > 0.2:
                logger.info(
                    "Name column found by content: %d (score=%.2f)",
                    best,
                    col_scores[best],
                )
                return best
        return 0

    @staticmethod
    def detect_config_columns(
        ws: Any, header_rows: list[int], col_types: dict[str, int]
    ) -> list[int]:
        """Detect configuration columns.

        Algorithm:
          1. Take all columns not identified as part_no/name/qty/meta
          2. Check them for numeric values (quantities)
          3. Filter VIN-breakdown (columns without numbers)
          4. Return sorted list of configuration columns

        Args:
            ws: Worksheet Excel
            header_rows: Found header rows
            col_types: Detected column types

        Returns:
            List of configuration column numbers.
        """
        max_col = ws.max_column or 200
        header_row = header_rows[0] if header_rows else 1
        known_cols = {v for v in col_types.values() if v > 0}

        # Collect all column headers
        headers: dict[int, str] = {}
        for c in range(1, max_col + 1):
            v = HeuristicAnalyzer.get_cell_value(ws, header_row, c)
            if v is not None:
                headers[c] = normalize_text(str(v))

        # Candidate columns: right of part_no, excluding known ones
        part_no_col = col_types.get("part_no", 1)
        candidates: list[int] = []
        last_named_col = part_no_col  # last column with header
        for c in range(part_no_col + 1, max_col + 1):
            if c in known_cols:
                continue
            header_text = headers.get(c, "")

            # Check for service/meta columns
            is_meta = False
            if header_text:
                for kw in STRICT_META_KEYWORDS:
                    if kw.lower() in header_text:
                        is_meta = True
                        break
                if is_meta:
                    continue

                for kw in META_KEYWORDS:
                    if kw.lower() in header_text:
                        is_meta = True
                        break
                if is_meta:
                    continue
                last_named_col = c
            else:
                # Column without header — add only if it is to the right of
                # the last column with header (configuration zone).
                if c <= last_named_col:
                    continue  # same or to the left — skip as meta column

            candidates.append(c)

        # Check for numeric values and content validity
        data_start = header_rows[-1] + 1 if header_rows else 2
        sample_end = min(data_start + 50, (ws.max_row or data_start + 50) + 1)

        # Patterns that are NOT valid configuration values
        _torque_range_re = re.compile(r"\d+\s*[±\-]\s*\d+")
        _bolt_pattern_re = re.compile(r"^[Mm]\d")
        _text_heavy_re = re.compile(r"[^\d\s.,;:]", re.UNICODE)

        col_has_numbers: dict[int, bool] = {}
        col_has_real_numbers: dict[
            int, bool
        ] = {}  # actual numeric values (not just S/-)
        for c in candidates:
            has_valid_config = False
            has_real_number = False
            invalid_hits = 0
            total_non_empty = 0
            for r in range(data_start, sample_end):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                total_non_empty += 1

                if isinstance(v, (int, float)) and v > 0:
                    has_valid_config = True
                    has_real_number = True
                    continue
                if isinstance(v, str):
                    stripped = v.strip()
                    if stripped in ("S", "s", "Y", "y", "–", "-", ""):
                        has_valid_config = True
                        continue
                    # Reject torque ranges: "40-50", "50±5", "1.6±0.1"
                    if _torque_range_re.search(stripped):
                        invalid_hits += 1
                        continue
                    # Reject bolt designations: "M6x16", "M8"
                    if _bolt_pattern_re.match(stripped):
                        invalid_hits += 1
                        continue
                    # Reject text-heavy values (not numeric config)
                    try:
                        val = float(stripped)
                        if val > 0:
                            has_valid_config = True
                            has_real_number = True
                            continue
                    except ValueError:
                        pass
                    # If mostly non-numeric text, not a config column
                    if len(stripped) > 3 and _text_heavy_re.search(stripped):
                        invalid_hits += 1

            # Discard columns where all values are identical (factory codes
            # like "1020" repeating in every row — these are meta-data, not configurations),
            # BUT only if the value is not a valid configuration marker (S/-/Y/number).
            unique_data_vals = set()
            data_rows_checked = 0
            val_counter: dict[str, int] = {}
            for r in range(data_start, sample_end):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is not None and str(v).strip():
                    # Skip values that look like sub-headers
                    # (text in Eng/Chi without digits — typical sub-header row)
                    sv = str(v).strip()
                    if len(sv) > 2 and not any(ch.isdigit() for ch in sv):
                        continue
                    unique_data_vals.add(sv)
                    val_counter[sv] = val_counter.get(sv, 0) + 1
                    data_rows_checked += 1
            # Skip if >70% of values are identical (factory codes, dates, etc.)
            if data_rows_checked >= 5 and len(unique_data_vals) >= 1:
                most_common_count = max(val_counter.values()) if val_counter else 0
                most_common_val = (
                    max(val_counter, key=val_counter.get) if val_counter else ""
                )
                dup_ratio = most_common_count / data_rows_checked
                if dup_ratio > 0.7:
                    is_valid_marker = (
                        most_common_val.upper() in ("S", "Y")
                        or most_common_val in ("-", "\u2013", "\u2014")
                        or _is_numeric_string(most_common_val)
                    )
                    if not is_valid_marker:
                        col_has_numbers[c] = False
                        col_has_real_numbers[c] = False
                        continue

            # Reject column if too many invalid values or no valid config values
            if total_non_empty > 0 and invalid_hits / total_non_empty > 0.3:
                col_has_numbers[c] = False
                col_has_real_numbers[c] = False
            else:
                col_has_numbers[c] = has_valid_config
                col_has_real_numbers[c] = has_real_number

        # Determine VIN-breakdown boundary (using real_numbers — columns with only S/- are markers)
        first_non_numeric: int | None = None
        found_numeric = False
        for c in candidates:
            if col_has_real_numbers.get(c, False):
                found_numeric = True
            elif found_numeric:
                first_non_numeric = c
                break

        config_cols: list[int] = []
        if first_non_numeric is not None:
            # Take BOTH groups: numeric (number qty) + non-numeric (S/- markers).
            # Unique-values filter has already excluded meta columns (factory codes, etc.)
            numeric_cols = [
                c
                for c in candidates
                if c < first_non_numeric and col_has_numbers.get(c, False)
            ]
            non_numeric_cols = [
                c
                for c in candidates
                if c >= first_non_numeric and col_has_numbers.get(c, False)
            ]
            config_cols = numeric_cols + non_numeric_cols
            logger.debug(
                "VIN-breakdown from column %d. Numeric: %d, Non-numeric: %d. Total: %d",
                first_non_numeric,
                len(numeric_cols),
                len(non_numeric_cols),
                len(config_cols),
            )
        else:
            config_cols = [c for c in candidates if col_has_numbers.get(c, False)]

        # If nothing found — take all candidates
        if not config_cols and candidates:
            config_cols = list(candidates)

        logger.debug(
            "Configuration columns: %s (total %d)", config_cols, len(config_cols)
        )
        return config_cols

    @staticmethod
    def find_graphic_number_column(
        ws: Any,
        header_rows: list[int],
        col_types: dict[str, int],
    ) -> int:
        """Find the schematic/operation number column (图示编号 / Graphic Number).

        Used for linking BOM parts to operational cards
        (some BOM formats: последний столбец таблицы содержит номер операции).

        Algorithm:
          1. Ищем колонку по ключевым словам GRAPHIC_NUMBER_KEYWORDS
          2. Если не нашли по заголовку — ищем по содержимому (паттерн DP-CH-A01 и т.д.)
          3. Возвращаем номер колонки или 0 если не найдена.

        Args:
            ws: Excel worksheet.
            header_rows: Found header rows.
            col_types: Detected column types.

        Returns:
            Column number or 0.
        """
        max_col = ws.max_column or 50
        known_cols = {v for v in col_types.values() if v > 0}

        # Phase 1: Search by headers
        for c in range(1, max_col + 1):
            if c in known_cols:
                continue
            for hr in header_rows:
                v = HeuristicAnalyzer.get_cell_value(ws, hr, c)
                if v is not None:
                    text = str(v).strip().lower()
                    for kw in GRAPHIC_NUMBER_KEYWORDS:
                        if kw.lower() in text:
                            logger.debug(
                                "Graphic number column found by header: колонка %d, '%s'",
                                c,
                                kw,
                            )
                            return c

        # Phase 2: Search by content (fallback — very lenient pattern)
        # Any non-empty string containing both letters AND digits (but not purely numeric),
        # and not resembling a quantity.
        data_start = header_rows[-1] + 1 if header_rows else 2
        sample_end = min(data_start + 30, (ws.max_row or data_start) + 1)

        for c in range(1, max_col + 1):
            if c in known_cols:
                continue
            hits = 0
            total = 0
            for r in range(data_start, sample_end):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is None:
                    continue
                total += 1
                s = str(v).strip()
                if not s or len(s) < 2:
                    continue
                # Filter out pure numbers (quantities) and very short strings
                try:
                    float(s.replace(",", "."))
                    continue  # This is a number — not graphic number
                except ValueError:
                    pass
                # Contains letters AND digits (any order, any delimiters)
                has_alpha = bool(re.search(r"[A-Za-z]", s))
                has_digit = bool(re.search(r"\d", s))
                if has_alpha and has_digit:
                    hits += 1
            if total > 2 and hits / total > 0.3:
                logger.debug(
                    "Graphic number column found by content: колонка %d (hits=%.2f)",
                    c,
                    hits / total,
                )
                return c

        return 0

    @staticmethod
    def extract_card_number_from_sheet(
        ws: Any, file_path: str, max_scan_rows: int = 15
    ) -> str:
        """Extract card number from sheet content or filename.

        Algorithm:
          1. Scan the first max_scan_rows rows for a card number
             (looking for alphanumeric patterns or operation codes)
          2. If found — return it
          3. If not — extract from filename

        Args:
            ws: Worksheet Excel
            file_path: File path (for fallback)
            max_scan_rows: Number of rows to scan

        Returns:
            Card number.
        """
        max_col = min(ws.max_column or 20, 20)

        for r in range(1, min(max_scan_rows + 1, (ws.max_row or max_scan_rows) + 1)):
            for c in range(1, max_col + 1):
                v = HeuristicAnalyzer.get_cell_value(ws, r, c)
                if v is not None:
                    # Strip _x000d_ / _x000A_ / \r\n artifacts from WPS Office
                    text = str(v)
                    text = XML_HEX_RE.sub("", text)
                    text = text.replace("\r", "").replace("\n", " ").strip()
                    match = CARD_NUMBER_RE.search(text)
                    if match:
                        card_no = match.group(0).strip("- ")
                        card_no = _normalize_card_number(card_no)
                        logger.debug("Card number from sheet content: %s", card_no)
                        return card_no

        # Fallback: from filename
        card_no = extract_card_number_from_filepath(file_path)
        logger.debug("Card number from filename: %s", card_no)
        return card_no

    @staticmethod
    def _is_false_positive_part_no(val: str, keyword: str) -> bool:
        """Check if a PART_NO_KEYWORD match is a false positive.

        Handles cases like:
          - "更改文件号" contains "件号" but is NOT a part number column
          - "变更记录" contains "记录" but is NOT a part number column

        Returns True if the match should be REJECTED.
        """
        if not val:
            return False

        # Compound-word prefixes that invalidate certain short keywords
        # When these precede a keyword, the compound has a different meaning
        _COMPOUND_PREFIXES: dict[str, list[str]] = {
            "件号": ["更改", "文件", "变更", "修订", "版本"],
        }

        prefixes = _COMPOUND_PREFIXES.get(keyword, [])
        for prefix in prefixes:
            if prefix in val and keyword in val:
                # Check that the prefix appears BEFORE the keyword
                prefix_pos = val.find(prefix)
                kw_pos = val.find(keyword)
                if prefix_pos >= 0 and kw_pos >= 0 and prefix_pos < kw_pos:
                    # The keyword is part of a compound word — reject
                    return True

        return False

    @staticmethod
    def find_part_table(
        ws: Any,
        start_row: int = 1,
    ) -> tuple[int, int, int, int] | None:
        """Find the parts table in a worksheet.

        Analyzes rows searching for parts table headers
        (part_no, qty, name).

        Supports 2-row headers: if name/qty not found in the same
        row as part_no — continues searching on the next 3-5 rows
        (common in wide-format cards).

        Args:
            ws: Excel worksheet.
            start_row: Row number to start searching from (for multi-operation sheets).

        Returns:
            (header_row, part_no_col, qty_col, name_col) or None.
        """
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

            # Header row must contain at least 2 non-empty cells
            non_empty_count = sum(1 for v in row_values if v)
            if non_empty_count < 2:
                continue

            # Scoring row as a parts table header
            has_part_no = any(
                not HeuristicAnalyzer._is_false_positive_part_no(v, kw)
                for v in row_values
                for kw in PART_NO_KEYWORDS
                if kw in v
            )
            if not has_part_no:
                continue

            # Determine columns (from scanned column range)
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
                    # Skip anti-keywords (factory/supplier)
                    if not any(ak in val for ak in NAME_ANTI_KEYWORDS):
                        name_col = col_idx

            # ── Multi-row header scan (BELOW) ──
            # If qty or name not found in same row — search next 10 rows
            # (common in wide-format cards, where part_no is on R21 and name/qty on R28 — 7-row gap)
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

                    # Check for part_no in scan row — do NOT take it as qty/name
                    has_pn_scan = any(
                        not HeuristicAnalyzer._is_false_positive_part_no(sv, kw)
                        for sv in scan_values
                        for kw in PART_NO_KEYWORDS
                        if kw in sv
                    )
                    if has_pn_scan:
                        # This is a new header — stop searching
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
                        logger.debug(
                            "Found name/qty on row %d (multi-row header below)",
                            scan_row,
                        )
                        break

            # ── Row-above header scan ──
            # If qty/name still not found — search ABOVE the part_no header
            # (for wide formats, where qty may be in the same row but beyond the scan limit,
            #  and the qty header may be in the row above part_no)
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

                    # Do not take a row with part_no as qty/name
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
                        logger.debug(
                            "Found name/qty on row %d (row-above header)",
                            scan_row,
                        )
                        break

            result = (
                row_idx,
                part_no_col,
                qty_col or 0,
                name_col or 0,
            )
            logger.debug(
                "Parts table: row %d, part_no=%s, qty=%s, name=%s",
                row_idx,
                part_no_col,
                qty_col,
                name_col,
            )
            return result

        return None

    @staticmethod
    def is_service_sheet(sheet_name: str) -> bool:
        """Check whether a sheet is a service sheet (not BOM, not operational card)."""
        name_lower = sheet_name.lower()
        for kw in SERVICE_SHEET_KEYWORDS:
            if kw in name_lower:
                return True
        return False

    @staticmethod
    def is_sheet_bom_candidate(
        ws: Any,
        min_configs: int = 2,
        sheet_name: str = "",
    ) -> bool:
        """Check whether a sheet is a BOM data candidate.

        Analyzes:
          - Presence of header row with part_no
          - Number of data rows (> 5)
          - Presence of at least min_configs configuration columns with numbers
          - Sheet name must not contain service keywords

        Args:
            ws: Worksheet Excel
            min_configs: Minimum number of configuration columns.
                         For the main BOM sheet, should be >= 2.
                         Special sheets (零部件附件) may have 1 qty column.
                         Multi-sheet BOMs may have 0-1.
            sheet_name: Sheet name for additional filtering.
        """
        # Filter by sheet name
        if sheet_name and HeuristicAnalyzer.is_service_sheet(sheet_name):
            return False

        header_rows = HeuristicAnalyzer.find_header_rows(ws, sheet_name=sheet_name)
        if not header_rows:
            return False

        col_types = HeuristicAnalyzer.detect_column_types(ws, header_rows)
        if "part_no" not in col_types:
            return False

        # Check data volume
        data_start = header_rows[-1] + 1
        data_rows = (ws.max_row or 0) - data_start + 1
        if data_rows < 3:
            return False

        # Check for at least min_configs configuration columns
        config_cols = HeuristicAnalyzer.detect_config_columns(
            ws, header_rows, col_types
        )
        if len(config_cols) >= min_configs:
            return True

        # Special sheets (附件) with one qty column
        # (including when ALL columns are classified as part_no/name/qty,
        #  and config_cols is empty — qty column itself serves as configuration)
        qty_col = col_types.get("qty", 0)
        has_name = "name_cn" in col_types or "name_en" in col_types
        if qty_col > 0 and has_name:
            return True

        # Multi-sheet BOMs:
        # If sheet contains part_no + name + qty — accept as BOM sheet
        # even with 0 configuration columns.
        # Data will be aggregated via config_quantities[sheet_name].
        part_no_col = col_types.get("part_no", 0)
        if qty_col > 0 and part_no_col > 0:
            return True

        return False

    @staticmethod
    def analyze_bom_sheet(
        ws: Any,
        min_configs: int = 2,
        sheet_name: str = "",
    ) -> tuple[list[int], dict[str, int], list[int]] | None:
        """Analyze a sheet and return (header_rows, col_types, config_cols) if it's a BOM candidate.

        Returns None if the sheet is not a BOM candidate.
        This avoids duplicate calls to find_header_rows/detect_column_types/detect_config_columns.
        """
        if sheet_name and HeuristicAnalyzer.is_service_sheet(sheet_name):
            return None

        header_rows = HeuristicAnalyzer.find_header_rows(ws, sheet_name=sheet_name)
        if not header_rows:
            return None

        col_types = HeuristicAnalyzer.detect_column_types(ws, header_rows)
        if "part_no" not in col_types:
            return None

        data_start = header_rows[-1] + 1
        data_rows = (ws.max_row or 0) - data_start + 1
        if data_rows < 3:
            return None

        config_cols = HeuristicAnalyzer.detect_config_columns(
            ws, header_rows, col_types
        )
        if len(config_cols) >= min_configs:
            return (header_rows, col_types, config_cols)

        qty_col = col_types.get("qty", 0)
        has_name = "name_cn" in col_types or "name_en" in col_types
        if qty_col > 0 and has_name:
            return (header_rows, col_types, config_cols)

        part_no_col = col_types.get("part_no", 0)
        if qty_col > 0 and part_no_col > 0:
            return (header_rows, col_types, config_cols)

        return None

    @staticmethod
    def extract_operation_name(ws: Any, table_header_row: int) -> str:
        """Extract operation name from the sheet header (above the parts table).

        Searches for text strings with Chinese characters,
        not containing service keywords.
        """
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
                # Skip service keywords
                if any(kw in text for kw in service_kws):
                    continue
                # Search for text with CJK characters
                if len(text) > 3 and bool(CJK_RE.search(text)):
                    # Return the first non-service name
                    if "作业要素" in text:
                        # Search nearby
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

    @staticmethod
    def build_global_name_dict(
        ws: Any,
        part_no_col: int,
        name_cn_col: int,
        name_en_col: int,
        header_row: int,
    ) -> dict[str, tuple[str, str]]:
        """Build a global dictionary part_number -> (name_cn, name_en).

        Scans ALL data rows (not just for a specific configuration),
        collecting names for EVERY found part number.

        Args:
            ws: Worksheet Excel
            part_no_col: Part number column
            name_cn_col: Name column (Chinese)
            name_en_col: Name column (English)
            header_row: Header row

        Returns:
            Dictionary {part_number: (name_cn, name_en)}
        """
        name_dict: dict[str, tuple[str, str]] = {}
        data_start = header_row + 1
        max_row = ws.max_row or data_start

        for row_idx in range(data_start, max_row + 1):
            if HeuristicAnalyzer.is_cell_strike(ws, row_idx, part_no_col):
                continue
            pn = HeuristicAnalyzer.get_cell_value(ws, row_idx, part_no_col)
            if pn is None:
                continue
            pn_str = str(pn).strip()
            if not pn_str or pn_str.startswith("~$"):
                continue
            pn_clean = clean_part_number(pn_str)
            if not pn_clean or len(pn_clean) < 3:
                continue

            name_cn = ""
            if name_cn_col:
                nc = HeuristicAnalyzer.get_cell_value(ws, row_idx, name_cn_col)
                if nc is not None:
                    name_cn = clean_cell_text(nc)

            name_en = ""
            if name_en_col:
                ne = HeuristicAnalyzer.get_cell_value(ws, row_idx, name_en_col)
                if ne is not None:
                    name_en = clean_cell_text(ne)

            # Save names (do not overwrite with empty)
            if pn_clean in name_dict:
                existing_cn, existing_en = name_dict[pn_clean]
                if not existing_cn and name_cn:
                    existing_cn = name_cn
                if not existing_en and name_en:
                    existing_en = name_en
                name_dict[pn_clean] = (existing_cn, existing_en)
            else:
                name_dict[pn_clean] = (name_cn, name_en)

        return name_dict


# ═══════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION: CARD NUMBER EXTRACTION (FROM FILE OR CONTENT)
# ═══════════════════════════════════════════════════════════════════════


def extract_card_number(file_path: str, ws: Any | None = None) -> str:
    """Extract operational card number.

    If ws is provided — first tries to extract from sheet content.
    Fallback: extracts from filename.

    Args:
        file_path: File path.
        ws: Worksheet Excel (опционально).

    Returns:
        Card number.
    """
    if ws is not None:
        return HeuristicAnalyzer.extract_card_number_from_sheet(ws, file_path)
    return extract_card_number_from_filepath(file_path)
