"""Unit tests for app/services/snapshot_service.py.

Covers:
  - extract_snapshot_from_bytes: normal XLSX, empty workbook, column cap, multi-sheet
  - group_by_format: AS-number pattern, trailing digits, no numeric suffix
  - _extract_group_key: various filename patterns
"""

from __future__ import annotations

import io

from openpyxl import Workbook

from app.services.snapshot_service import (
    _extract_group_key,
    extract_snapshot_from_bytes,
    group_by_format,
)

# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════


def _make_xlsx_bytes(
    sheets: dict[str, list[list[str | None]]] | None = None,
) -> bytes:
    """Create a minimal XLSX in memory and return raw bytes."""
    wb = Workbook()
    if sheets:
        # Remove default sheet if explicit sheets provided
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


# ═══════════════════════════════════════════════════════════════════════
#  extract_snapshot_from_bytes
# ═══════════════════════════════════════════════════════════════════════


class TestExtractSnapshotFromBytes:
    def test_basic_snapshot(self):
        """Normal XLSX with headers and 2 data rows."""
        data = _make_xlsx_bytes(
            {
                "Sheet1": [
                    ["Part No", "Qty", "Name"],
                    ["P001", "2", "Bolt"],
                    ["P002", "1", "Nut"],
                ],
            }
        )
        result = extract_snapshot_from_bytes(data, "test.xlsx")
        assert result["filename"] == "test.xlsx"
        assert len(result["sheets"]) == 1
        sheet = result["sheets"][0]
        assert sheet["name"] == "Sheet1"
        assert sheet["max_row"] == 3
        assert len(sheet["rows"]) == 3
        assert sheet["rows"][0] == ["Part No", "Qty", "Name"]
        assert sheet["rows"][1] == ["P001", "2", "Bolt"]

    def test_multi_sheet(self):
        """Multi-sheet workbook — each sheet is captured."""
        data = _make_xlsx_bytes(
            {
                "Op1": [["Header"], ["Data1"]],
                "Op2": [["Col"], ["Data2"]],
            }
        )
        result = extract_snapshot_from_bytes(data, "multi.xlsx")
        assert len(result["sheets"]) == 2
        names = [s["name"] for s in result["sheets"]]
        assert "Op1" in names
        assert "Op2" in names

    def test_max_rows_capped(self):
        """Only first max_rows rows are captured."""
        rows = [["R" + str(i), str(i)] for i in range(100)]
        data = _make_xlsx_bytes({"S": rows})
        result = extract_snapshot_from_bytes(data, "big.xlsx", max_rows=10)
        assert len(result["sheets"][0]["rows"]) == 10

    def test_column_cap_at_200(self):
        """Columns are capped at 200 even if sheet has more."""
        wide_row = [f"Col{i}" for i in range(250)]
        wb = Workbook()
        ws = wb.active
        for c_idx, val in enumerate(wide_row, 1):
            ws.cell(row=1, column=c_idx, value=val)
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        data = buf.getvalue()

        result = extract_snapshot_from_bytes(data, "wide.xlsx")
        sheet = result["sheets"][0]
        assert sheet["max_column"] == 200

    def test_empty_workbook(self):
        """Empty workbook with no data."""
        data = _make_xlsx_bytes()
        result = extract_snapshot_from_bytes(data, "empty.xlsx")
        assert len(result["sheets"]) >= 1
        # Default empty sheet
        assert result["sheets"][0]["rows"] == []

    def test_numeric_and_boolean_values(self):
        """Numeric and boolean cell values are preserved."""
        wb = Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value=42)
        ws.cell(row=1, column=2, value=3.14)
        ws.cell(row=1, column=3, value=True)
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()

        result = extract_snapshot_from_bytes(buf.getvalue(), "types.xlsx")
        row = result["sheets"][0]["rows"][0]
        assert row[0] == 42
        assert row[1] == 3.14
        assert row[2] is True

    def test_none_cells_excluded(self):
        """Empty cells are represented as None in the snapshot."""
        wb = Workbook()
        ws = wb.active
        ws.cell(row=1, column=1, value="A")
        # Column 2 is left empty
        ws.cell(row=1, column=3, value="C")
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()

        result = extract_snapshot_from_bytes(buf.getvalue(), "sparse.xlsx")
        row = result["sheets"][0]["rows"][0]
        assert row[0] == "A"
        assert row[1] is None
        assert row[2] == "C"


# ═══════════════════════════════════════════════════════════════════════
#  _extract_group_key
# ═══════════════════════════════════════════════════════════════════════


class TestExtractGroupKey:
    def test_as_number_pattern(self):
        assert _extract_group_key("SQRT1L-17-AS-04001").upper() == "SQRT1L-17-AS-"

    def test_trailing_digits(self):
        assert _extract_group_key("card_001") == "card_"

    def test_no_numeric_suffix(self):
        assert _extract_group_key("template") == "template"

    def test_only_digits(self):
        """Basename that is entirely digits — stripped prefix is empty, falls back to basename."""
        result = _extract_group_key("12345")
        # stripped = "" (all digits) → empty string is falsy → returns basename
        assert result == "12345"

    def test_mixed_prefix(self):
        key = _extract_group_key("G01-A-AS-05001")
        assert "G01" in key


# ═══════════════════════════════════════════════════════════════════════
#  group_by_format
# ═══════════════════════════════════════════════════════════════════════


class TestGroupByFormat:
    def test_as_number_grouping(self):
        paths = [
            "/data/SQRT-AS-001.xlsx",
            "/data/SQRT-AS-002.xlsx",
            "/data/SQRT-AS-003.xlsx",
        ]
        groups = group_by_format(paths)
        assert len(groups) == 1
        key = list(groups.keys())[0]
        assert len(groups[key]) == 3

    def test_different_prefixes(self):
        paths = [
            "/data/card_001.xlsx",
            "/data/card_002.xlsx",
            "/data/report_001.xlsx",
        ]
        groups = group_by_format(paths)
        assert len(groups) == 2

    def test_no_numeric_suffix(self):
        paths = ["/data/template.xlsx", "/data/cover.xlsx"]
        groups = group_by_format(paths)
        assert len(groups) == 2

    def test_empty_list(self):
        assert group_by_format([]) == {}

    def test_single_file(self):
        groups = group_by_format(["/data/001-card.xlsx"])
        assert len(groups) == 1
