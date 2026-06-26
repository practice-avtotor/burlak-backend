"""Unit tests for app/services/card_parser_service.py.

Covers:
  - classify_file: service, operational_card, unknown, with custom rules
  - CardParserService.parse_card: operational, service, unknown, error cases
  - CardParserService.extract_unique_strings: normal, no name column, service
  - Edge cases: end_markers, empty rows, invalid part numbers
"""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.services.card_parser_service import (
    CardParserService,
    classify_file,
)

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_xlsx_bytes(
    sheets: dict[str, list[list[str | int | float | None]]] | None = None,
) -> bytes:
    """Create a minimal XLSX in memory and return raw bytes."""
    wb = Workbook()
    if sheets:
        wb.remove(wb.active)
        for name, rows in sheets.items():
            ws = wb.create_sheet(title=name)
            for r_idx, row in enumerate(rows, 1):
                for c_idx, val in enumerate(row, 1):
                    if val is not None:
                        ws.cell(row=r_idx, column=c_idx, value=val)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _default_mapping_config() -> dict:
    """Return a minimal but complete mapping config for testing.

    Note: header_row=1, data_start_row=2 means row 1 is the header
    and data begins at row 2.
    """
    return {
        "cards": {
            "file_classification_rules": {
                "service_keywords": ["封面", "目录", "template"],
                "operational_keywords": ["作业指导书"],
            },
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
                "default_type": "operational",
            },
        }
    }


# ═══════════════════════════════════════════════════════════════════════
#  classify_file
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyFile:
    def test_service_file_cover(self):
        assert classify_file("封面.xlsx") == "service"

    def test_service_file_toc(self):
        assert classify_file("目录.xlsx") == "service"

    def test_service_file_template(self):
        assert classify_file("template_cover.xlsx") == "service"

    def test_operational_keyword(self):
        assert classify_file("作业指导书_001.xlsx") == "operational_card"

    def test_operational_digit_prefix(self):
        assert classify_file("001-card.xlsx") == "operational_card"

    def test_operational_letter_digit_prefix(self):
        assert classify_file("A001-assembly.xlsx") == "operational_card"

    def test_operational_as_pattern(self):
        assert classify_file("SQRT1L-A-AS-04001.xlsx") == "operational_card"

    def test_unknown_file(self):
        assert classify_file("random_report.xlsx") == "unknown"

    def test_custom_rules_override(self):
        rules = {
            "service_keywords": ["custom_svc"],
            "operational_keywords": ["custom_op"],
        }
        assert classify_file("custom_svc_file.xlsx", rules) == "service"
        assert classify_file("custom_op_file.xlsx", rules) == "operational_card"

    def test_service_keyword_takes_priority(self):
        """Even if filename contains operational keyword, service wins."""
        assert classify_file("CP7作业指导书封面及目录.xlsx") == "service"


# ═══════════════════════════════════════════════════════════════════════
#  CardParserService.parse_card
# ═══════════════════════════════════════════════════════════════════════


class TestParseCard:
    def test_operational_card_basic(self):
        """Normal operational card with header row + 3 data rows."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "Sheet1": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["P001", "Bolt", 2],  # row 2 — data
                    ["P002", "Nut", 1],  # row 3 — data
                    ["P003", "Washer", 3],  # row 4 — data
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")

        assert result.file_type == "operational_card"
        assert len(result.parts) == 3
        assert result.aggregated_parts["P001"] == 2.0
        assert result.aggregated_parts["P002"] == 1.0
        assert result.aggregated_parts["P003"] == 3.0
        assert result.sheets_parsed == 1

    def test_service_file_skipped(self):
        """Service files return empty result without parsing."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes({"S": [["Data", "here"]]})
        result = parser.parse_card(data, "封面.xlsx")

        assert result.file_type == "service"
        assert result.parts == []
        assert result.aggregated_parts == {}

    def test_unknown_file_skipped(self):
        """Unknown files return empty result."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes({"S": [["Data"]]})
        result = parser.parse_card(data, "random.xlsx")

        assert result.file_type == "unknown"
        assert result.parts == []

    def test_part_number_normalization(self):
        """Part numbers are cleaned and normalized."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["5306200-ED001", "Bolt", 2],  # row 2 — data
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert "5306200ED001" in result.aggregated_parts
        assert result.original_part_numbers["5306200ED001"] == "5306200-ED001"

    def test_invalid_part_numbers_filtered(self):
        """Invalid part numbers (too short, garbage) are filtered out."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["AB", "Too Short", 1],  # row 2 — invalid (too short)
                    ["P001", "Valid Part", 2],  # row 3 — valid
                    ["无", "None", 1],  # row 4 — garbage
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 1
        assert result.parts[0].part_number == "P001"

    def test_end_markers_stop_parsing(self):
        """Parsing stops when an end marker is encountered."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["P001", "Bolt", 1],  # row 2 — data
                    ["P002", "Nut", 2],  # row 3 — data
                    ["签字", "", ""],  # row 4 — end marker
                    ["P003", "Washer", 3],  # row 5 — should NOT be collected
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 2
        assert "P003" not in result.aggregated_parts

    def test_three_empty_rows_stop(self):
        """Parsing stops after 3 consecutive empty rows in part_no column."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["P001", "Bolt", 1],  # row 2 — data
                    [None, None, None],  # row 3 — empty 1
                    [None, None, None],  # row 4 — empty 2
                    [None, None, None],  # row 5 — empty 3 → stop
                    ["P002", "Nut", 2],  # row 6 — should NOT be collected
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 1
        assert result.parts[0].part_number == "P001"

    def test_duplicate_parts_aggregated(self):
        """Duplicate part numbers have quantities summed."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["P001", "Bolt", 1],  # row 2 — data
                    ["P001", "Bolt", 2],  # row 3 — duplicate
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.aggregated_parts["P001"] == 3.0

    def test_multi_sheet(self):
        """Parts from multiple sheets are collected."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "Op1": [
                    ["Part No", "Name", "Qty"],  # header
                    ["P001", "Bolt", 1],  # data
                ],
                "Op2": [
                    ["Part No", "Name", "Qty"],  # header
                    ["P002", "Nut", 2],  # data
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 2
        assert result.sheets_parsed == 2

    def test_default_qty_when_no_qty_column(self):
        """When qty column index is 0, all parts get qty=1.0."""
        cfg = _default_mapping_config()
        cfg["cards"]["columns"]["qty"] = 0
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name"],  # header
                    ["P001", "Bolt"],  # data
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 1
        assert result.parts[0].quantity == 1.0

    def test_missing_part_no_column_returns_error(self):
        """If mapping config has no part_no column, returns error result."""
        cfg = _default_mapping_config()
        cfg["cards"]["columns"]["part_no"] = 0
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Header"],
                    ["P001"],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.error is not None
        assert "part_no" in result.error

    def test_corrupted_xlsx_raises(self):
        """Non-XLSX bytes raise an exception for operational card filenames."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        # Use an operational card filename so it proceeds to parse
        with pytest.raises(Exception):
            parser.parse_card(b"not an xlsx file", "001-card.xlsx")

    def test_name_from_name_column(self):
        """Name values are read from the name column."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],
                    ["P001", "螺栓 M6×20", 5],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.parts[0].name == "螺栓 M6×20"

    def test_row_tracking(self):
        """Each ParsedPart tracks its source row number."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # row 1 — header
                    ["P001", "Bolt", 1],  # row 2
                    ["P002", "Nut", 2],  # row 3
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.parts[0].row == 2
        assert result.parts[1].row == 3

    def test_skip_whitespace_part_no(self):
        """Whitespace-only part numbers are skipped."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],
                    ["   ", "Empty", 1],  # whitespace-only → skipped
                    ["P001", "Valid", 2],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 1
        assert result.parts[0].part_number == "P001"

    def test_end_marker_in_name_column(self):
        """End markers in the name column (when part_no is None) stop parsing."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],
                    ["P001", "Bolt", 1],
                    [None, "签字确认", None],  # end marker in name column
                    ["P002", "Nut", 2],  # NOT collected
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 1


# ═══════════════════════════════════════════════════════════════════════
#  CardParserService.extract_unique_strings
# ═══════════════════════════════════════════════════════════════════════


class TestExtractUniqueStrings:
    def test_basic_extraction(self):
        """Extracts unique strings from the name column."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # header
                    ["P001", "螺栓", 1],
                    ["P002", "螺母", 2],
                    ["P003", "螺栓", 1],  # duplicate "螺栓"
                ],
            }
        )
        result = parser.extract_unique_strings(data, "001-card.xlsx")
        assert "螺栓" in result
        assert "螺母" in result
        assert len(result) == 2  # deduplicated

    def test_service_file_returns_empty(self):
        """Service files return empty list."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes({"S": [["Data"]]})
        result = parser.extract_unique_strings(data, "封面.xlsx")
        assert result == []

    def test_no_name_column_returns_empty(self):
        """When name column is 0, returns empty list."""
        cfg = _default_mapping_config()
        cfg["cards"]["columns"]["name"] = 0
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Header"],
                    ["P001"],
                ],
            }
        )
        result = parser.extract_unique_strings(data, "001-card.xlsx")
        assert result == []

    def test_end_markers_stop_extraction(self):
        """End markers in name column stop extraction."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],  # header (row 1)
                    ["P001", "螺栓", 1],  # row 2 → collected
                    ["P002", "签字", 2],  # row 3 → end marker → stop
                    ["P003", "螺母", 3],  # row 4 → NOT collected
                ],
            }
        )
        result = parser.extract_unique_strings(data, "001-card.xlsx")
        assert "螺栓" in result
        assert "螺母" not in result

    def test_empty_strings_excluded(self):
        """Empty or whitespace-only names are excluded."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],
                    ["P001", "  ", 1],
                    ["P002", "", 2],
                    ["P003", "Bolt", 3],
                ],
            }
        )
        result = parser.extract_unique_strings(data, "001-card.xlsx")
        assert result == ["Bolt"]


# ═══════════════════════════════════════════════════════════════════════
#  CardParserService.classify
# ═══════════════════════════════════════════════════════════════════════


class TestParserClassify:
    def test_uses_config_rules(self):
        """Parser uses classification rules from mapping config."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)
        assert parser.classify("封面.xlsx") == "service"
        assert parser.classify("001-card.xlsx") == "operational_card"
        assert parser.classify("random.xlsx") == "unknown"

    def test_no_rules_uses_defaults(self):
        """When mapping_config has no classification rules, defaults are used."""
        cfg = {"cards": {"columns": {}, "table_boundaries": {}}}
        parser = CardParserService(cfg)
        # Should still classify using built-in defaults
        assert parser.classify("封面.xlsx") == "service"
