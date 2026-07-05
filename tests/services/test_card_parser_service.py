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

    def test_corrupted_xlsx_returns_error_result(self):
        """Non-XLSX bytes return an error result instead of raising."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        # Use an operational card filename so it proceeds to parse
        result = parser.parse_card(b"not an xlsx file", "001-card.xlsx")
        assert result.error is not None
        assert "Cannot open workbook" in result.error

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

    def test_validation_column_index_out_of_bounds(self):
        """If column index is out of bounds, falls back to auto-detection."""
        cfg = _default_mapping_config()
        cfg["cards"]["columns"]["part_no"] = 100  # out of bounds
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],
                    ["P001", "Bolt", 1],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        # Auto-detect finds headers and parses successfully
        assert len(result.parts) > 0
        assert result.parts[0].part_number == "P001"

    def test_validation_zero_parts_extracted_from_non_empty_sheet(self):
        """If 0 parts are parsed from a non-empty sheet, returns validation error."""
        cfg = _default_mapping_config()
        parser = CardParserService(cfg)

        # Worksheet has content, but all part numbers are invalid (e.g. empty or garbage)
        # We need at least 10 rows with some content to trigger validation_error
        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Some", "Other", "Headers"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                    ["Val", "Val", "Val"],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert len(result.parts) == 0
        assert (
            result.error
            == "No parts extracted from non-empty worksheet. Check mapping config columns."
        )


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
        CardParserService(cfg)


# ═══════════════════════════════════════════════════════════════════════
#  Multi-card sheet tests
# ═══════════════════════════════════════════════════════════════════════


class TestMultiCard:
    """Tests for multi-card sheets (multiple cards stacked vertically)."""

    def _multi_card_config(self) -> dict:
        """Return mapping config with multi_card table_boundaries."""
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
                    "type": "multi_card",
                    "multi_card": {
                        "separator_type": "empty_rows",
                        "empty_rows_separator": 1,
                        "has_repeating_header": True,
                        "parts_header_row": 1,
                        "parts_data_start_row": 1,
                        "max_cards": 0,
                        "card_end_markers": ["签字", "审核"],
                    },
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

    def test_two_cards_separated_by_empty_row(self):
        """Two cards separated by one empty row — both should be parsed."""
        cfg = self._multi_card_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    # Card 1
                    ["Part No", "Name", "Qty"],
                    ["P001", "Bolt", 2],
                    ["P002", "Nut", 4],
                    # empty separator row
                    [None, None, None],
                    # Card 2
                    ["Part No", "Name", "Qty"],
                    ["P003", "Washer", 6],
                    ["P004", "Screw", 1],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.error is None, f"Unexpected error: {result.error}"
        assert len(result.parts) == 4
        assert result.card_boundaries is not None
        assert len(result.card_boundaries) == 2

        # Card 1 boundary: (sheet_name, start, end)
        sheet1, start1, end1 = result.card_boundaries[0]
        assert sheet1 == "S"
        assert start1 == 2  # first data row
        assert end1 == 3  # last data row of card 1

        # Card 2 boundary
        sheet2, start2, end2 = result.card_boundaries[1]
        assert sheet2 == "S"
        assert start2 == 5  # first row of card 2 (header)
        assert end2 == 7  # last data row of card 2

        # Verify parts belong to correct cards
        card1_parts = [
            p
            for p in result.parts
            if p.source_sheet == sheet1 and start1 <= p.row <= end1
        ]
        card2_parts = [
            p
            for p in result.parts
            if p.source_sheet == sheet2 and start2 <= p.row <= end2
        ]
        assert len(card1_parts) == 2
        assert len(card2_parts) == 2
        assert card1_parts[0].part_number == "P001"
        assert card2_parts[0].part_number == "P003"

    def test_three_cards_different_sizes(self):
        """Three cards of different sizes — verifies variable-length card support."""
        cfg = self._multi_card_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    # Card 1 — 2 parts
                    ["Part No", "Name", "Qty"],
                    ["P001", "Bolt", 1],
                    ["P002", "Nut", 2],
                    # separator
                    [None, None, None],
                    # Card 2 — 4 parts
                    ["Part No", "Name", "Qty"],
                    ["P003", "Washer", 3],
                    ["P004", "Screw", 4],
                    ["P005", "Pin", 5],
                    ["P006", "Clip", 6],
                    # separator
                    [None, None, None],
                    # Card 3 — 1 part
                    ["Part No", "Name", "Qty"],
                    ["P007", "Spring", 7],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.error is None
        assert len(result.parts) == 7
        assert result.card_boundaries is not None
        assert len(result.card_boundaries) == 3

        # Verify counts per card
        # Card 1: rows 2-3 (P001, P002) = 2 parts
        # Card 2: rows 6-8 (P003-P006) = 4 parts (header at row 5 skipped)
        # Card 3: row 11 (P007) = 1 part (header at row 10 skipped)
        for (sh, s, e), expected_count in zip(result.card_boundaries, [2, 4, 1]):
            assert sh == "S"
            card_parts = [
                p for p in result.parts if p.source_sheet == sh and s <= p.row <= e
            ]
            assert len(card_parts) == expected_count, (
                f"Card ({sh},{s},{e}): expected {expected_count} parts, got {len(card_parts)}"
            )

    def test_multi_card_with_end_marker(self):
        """End marker stops current card, next card starts after separator."""
        cfg = self._multi_card_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    # Card 1
                    ["Part No", "Name", "Qty"],
                    ["P001", "Bolt", 2],
                    ["签字确认", None, None],  # end marker
                    # separator
                    [None, None, None],
                    # Card 2
                    ["Part No", "Name", "Qty"],
                    ["P002", "Nut", 4],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.error is None
        # Both cards are parsed: P001 from card 1, P002 from card 2
        assert len(result.parts) == 2
        assert result.parts[0].part_number == "P001"
        assert result.parts[1].part_number == "P002"
        assert result.card_boundaries is not None
        assert len(result.card_boundaries) == 2

        # Card 1 should end at the end marker row
        sh1, start1, end1 = result.card_boundaries[0]
        assert sh1 == "S"
        assert end1 == 2  # P001 row, end marker excluded

    def test_multi_card_single_card_falls_back(self):
        """A multi_card config with only one card should still work (card_boundaries has 1 entry)."""
        cfg = self._multi_card_config()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes(
            {
                "S": [
                    ["Part No", "Name", "Qty"],
                    ["P001", "Bolt", 2],
                    ["P002", "Nut", 4],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.error is None
        assert len(result.parts) == 2
        assert result.card_boundaries is not None
        assert len(result.card_boundaries) == 1
        # Should still classify using built-in defaults
        assert parser.classify("封面.xlsx") == "service"


# ═══════════════════════════════════════════════════════════════════════
#  classify_file_with_format
# ═══════════════════════════════════════════════════════════════════════


class TestClassifyFileWithFormat:
    """Tests for classify_file_with_format with the new schema."""

    def test_legacy_schema_returns_none_format(self):
        """Legacy schema (service_keywords/operational_keywords) returns None format_group."""
        from app.services.card_parser_service import classify_file_with_format

        rules = {
            "service_keywords": ["封面"],
            "operational_keywords": ["作业指导书"],
        }
        assert classify_file_with_format("封面.xlsx", rules) == ("service", None)
        assert classify_file_with_format("作业指导书_001.xlsx", rules) == (
            "operational_card",
            None,
        )

    def test_new_schema_filename_regex_with_format_group(self):
        """New schema filename_regex returns the correct format_group."""
        from app.services.card_parser_service import classify_file_with_format

        rules = {
            "operational_card_patterns": [
                {
                    "type": "filename_regex",
                    "pattern": "^[A-Za-z0-9]+-[A-Za-z0-9]*-AS-\\d+",
                    "format_group": "card_format_A",
                },
                {
                    "type": "filename_regex",
                    "pattern": "^\\d{2,}",
                    "format_group": "card_format_B",
                },
            ],
            "service_file_patterns": [
                {
                    "type": "filename_keyword",
                    "keywords": ["封面", "目录"],
                },
            ],
        }

        # Matches first pattern → card_format_A
        assert classify_file_with_format("SQRT1L-A-AS-04001.xlsx", rules) == (
            "operational_card",
            "card_format_A",
        )
        # Matches second pattern → card_format_B
        assert classify_file_with_format("001-card.xlsx", rules) == (
            "operational_card",
            "card_format_B",
        )
        # Service file → service, no format_group
        assert classify_file_with_format("封面.xlsx", rules) == ("service", None)
        # Unknown file → unknown, no format_group
        assert classify_file_with_format("random.xlsx", rules) == ("unknown", None)

    def test_new_schema_sheet_keyword_with_format_group(self):
        """New schema sheet_keyword returns the correct format_group."""
        from app.services.card_parser_service import classify_file_with_format

        rules = {
            "operational_card_patterns": [
                {
                    "type": "sheet_keyword",
                    "keywords": ["作业指导书", "工艺卡"],
                    "format_group": "card_format_X",
                },
            ],
            "service_file_patterns": [
                {
                    "type": "filename_keyword",
                    "keywords": ["封面"],
                },
            ],
        }

        assert classify_file_with_format("作业指导书_001.xlsx", rules) == (
            "operational_card",
            "card_format_X",
        )
        assert classify_file_with_format("工艺卡_100.xlsx", rules) == (
            "operational_card",
            "card_format_X",
        )

    def test_new_schema_service_takes_priority(self):
        """Service patterns take priority over operational patterns."""
        from app.services.card_parser_service import classify_file_with_format

        rules = {
            "operational_card_patterns": [
                {
                    "type": "filename_regex",
                    "pattern": "^\\d{2,}",
                    "format_group": "card_format_A",
                },
            ],
            "service_file_patterns": [
                {
                    "type": "filename_keyword",
                    "keywords": ["封面", "目录"],
                },
            ],
        }

        # Filename matches both operational regex AND service keyword
        # Service should win
        assert classify_file_with_format("001封面.xlsx", rules) == ("service", None)

    def test_new_schema_heuristic_fallback(self):
        """When no patterns match, heuristic fallbacks still work but return None format."""
        from app.services.card_parser_service import classify_file_with_format

        rules = {
            "operational_card_patterns": [],
            "service_file_patterns": [],
        }

        # Heuristic: digit prefix
        assert classify_file_with_format("001-card.xlsx", rules) == (
            "operational_card",
            None,
        )
        # Heuristic: AS pattern
        assert classify_file_with_format("SQRT1L-A-AS-04001.xlsx", rules) == (
            "operational_card",
            None,
        )

    def test_no_rules_uses_defaults(self):
        """When classification_rules is None, defaults are used and format_group is None."""
        from app.services.card_parser_service import classify_file_with_format

        assert classify_file_with_format("封面.xlsx", None) == ("service", None)
        assert classify_file_with_format("001-card.xlsx", None) == (
            "operational_card",
            None,
        )
        assert classify_file_with_format("random.xlsx", None) == ("unknown", None)


# ═══════════════════════════════════════════════════════════════════════
#  CardParserService with new formats schema
# ═══════════════════════════════════════════════════════════════════════


class TestParserWithFormats:
    """Tests for CardParserService with the new cards.formats schema."""

    def _mapping_with_two_formats(self) -> dict:
        """Return a mapping_config with two formats and classification rules."""
        return {
            "cards": {
                "file_classification_rules": {
                    "operational_card_patterns": [
                        {
                            "type": "filename_regex",
                            "pattern": "^[A-Za-z0-9]+-[A-Za-z0-9]*-AS-\\d+",
                            "format_group": "card_format_A",
                        },
                        {
                            "type": "filename_regex",
                            "pattern": "^\\d{2,}",
                            "format_group": "card_format_B",
                        },
                    ],
                    "service_file_patterns": [
                        {
                            "type": "filename_keyword",
                            "keywords": ["封面", "目录"],
                        },
                    ],
                },
                "formats": {
                    "card_format_A": {
                        "structure_type": "standard_table",
                        "sheets": [
                            {
                                "sheet_name": None,
                                "sheet_type": "card_data",
                                "header_rows": [1],
                                "data_start_row": 2,
                                "columns": {
                                    "part_no": {"col_index": 2},
                                    "name_cn": {"col_index": 3},
                                    "qty": {"col_index": 4},
                                },
                                "table_boundaries": {
                                    "type": "end_markers",
                                    "markers": ["签字", "审核"],
                                    "empty_rows_threshold": 3,
                                },
                            }
                        ],
                    },
                    "card_format_B": {
                        "structure_type": "standard_table",
                        "sheets": [
                            {
                                "sheet_name": None,
                                "sheet_type": "card_data",
                                "header_rows": [1],
                                "data_start_row": 2,
                                "columns": {
                                    "part_no": {"col_index": 18},
                                    "name_cn": {"col_index": 0},
                                    "qty": {"col_index": 0},
                                },
                                "table_boundaries": {
                                    "type": "multi_card",
                                    "multi_card": {
                                        "separator_type": "empty_row",
                                        "empty_rows_separator": 1,
                                        "has_repeating_header": True,
                                        "parts_header_row": 1,
                                        "parts_data_start_row": 1,
                                        "max_cards": 0,
                                    },
                                },
                            }
                        ],
                    },
                },
            },
        }

    def test_format_a_selected_for_as_pattern(self):
        """Files matching card_format_A pattern use card_format_A columns (part_no=2)."""
        cfg = self._mapping_with_two_formats()
        parser = CardParserService(cfg)

        # SQRT1L-A-AS-04001 matches card_format_A → part_no=2, name=3, qty=4
        data = _make_xlsx_bytes(
            {
                "Sheet1": [
                    ["H1", "Part No", "Name", "Qty"],  # row 1 — header
                    ["X", "P001", "Bolt", 2],  # row 2 — data
                    ["Y", "P002", "Nut", 1],  # row 3 — data
                ],
            }
        )
        result = parser.parse_card(data, "SQRT1L-A-AS-04001.xlsx")
        assert result.error is None, f"Unexpected error: {result.error}"
        assert len(result.parts) == 2
        assert result.parts[0].part_number == "P001"
        assert result.parts[1].part_number == "P002"

    def test_format_b_selected_for_digit_prefix(self):
        """Files matching card_format_B pattern use card_format_B columns (part_no=18)."""
        cfg = self._mapping_with_two_formats()
        parser = CardParserService(cfg)

        # 001-card matches card_format_B → part_no column index is 18
        # card_format_B has multi_card type with parts_data_start_row=2
        # actual_data_start = card_start + parts_data_start - 1 = 1 + 2 - 1 = 2
        # So row 1 is the card header, data starts at row 2
        data = _make_xlsx_bytes(
            {
                "Sheet1": [
                    # Card 1 header row (row 1, skipped by parts_data_start_row=2)
                    [None] * 17 + ["Header"],
                    # Card 1 data rows (part_no at col 18)
                    [None] * 17 + ["P001", "Bolt", 2],
                    [None] * 17 + ["P002", "Nut", 1],
                ],
            }
        )
        result = parser.parse_card(data, "001-card.xlsx")
        assert result.error is None, f"Unexpected error: {result.error}"
        assert len(result.parts) == 2
        assert result.parts[0].part_number == "P001"
        assert result.parts[1].part_number == "P002"

    def test_service_file_still_skipped(self):
        """Service files return empty result regardless of formats."""
        cfg = self._mapping_with_two_formats()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes({"S": [["Data"]]})
        result = parser.parse_card(data, "封面.xlsx")
        assert result.file_type == "service"
        assert result.parts == []

    def test_unknown_file_still_skipped(self):
        """Unknown files return empty result regardless of formats."""
        cfg = self._mapping_with_two_formats()
        parser = CardParserService(cfg)

        data = _make_xlsx_bytes({"S": [["Data"]]})
        result = parser.parse_card(data, "random.xlsx")
        assert result.file_type == "unknown"
        assert result.parts == []

    def test_classify_with_format_returns_correct_group(self):
        """Parser.classify_with_format returns the correct format_group."""
        cfg = self._mapping_with_two_formats()
        parser = CardParserService(cfg)

        assert parser.classify_with_format("SQRT1L-A-AS-04001.xlsx") == (
            "operational_card",
            "card_format_A",
        )
        assert parser.classify_with_format("001-card.xlsx") == (
            "operational_card",
            "card_format_B",
        )
        assert parser.classify_with_format("封面.xlsx") == ("service", None)
        assert parser.classify_with_format("random.xlsx") == ("unknown", None)

    def test_classify_still_returns_just_type(self):
        """Parser.classify still returns just the file type string."""
        cfg = self._mapping_with_two_formats()
        parser = CardParserService(cfg)

        assert parser.classify("SQRT1L-A-AS-04001.xlsx") == "operational_card"
        assert parser.classify("001-card.xlsx") == "operational_card"
        assert parser.classify("封面.xlsx") == "service"
        assert parser.classify("random.xlsx") == "unknown"
