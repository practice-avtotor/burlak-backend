"""Excel snapshot extraction service.

Reads XLSX workbooks entirely in memory (via io.BytesIO) and extracts
compact JSON snapshots of sheet structure for ML analysis. Also provides
file grouping by naming patterns to reduce redundant ML calls.

Critical constraints:
  - All operations are in-memory — no disk I/O.
  - Column count is capped at 50 to prevent memory issues with huge sheets.
"""

from __future__ import annotations

import io
import logging
import os
from collections import defaultdict
from typing import Any

import openpyxl

logger = logging.getLogger(__name__)

# Maximum columns to scan per sheet (safety cap)
_MAX_COLUMNS = 200


def extract_snapshot_from_bytes(
    data: bytes,
    filename: str,
    max_rows: int = 300,
) -> dict[str, Any]:
    """Extract a compact JSON snapshot of an XLSX workbook from raw bytes.

    Opens the workbook entirely in memory via ``openpyxl.load_workbook``
    with ``io.BytesIO``. Gathers the first *max_rows* rows of each sheet
    (capped at 50 columns) so the ML service can determine table layout
    without seeing the full file.

    Args:
        data: Raw bytes of the XLSX file.
        filename: Original filename (used for logging/metadata only).
        max_rows: Maximum number of rows to capture per sheet.

    Returns:
        A dict with keys:
          - ``filename``: original filename.
          - ``sheets``: list of per-sheet snapshots, each containing:
              - ``name``: sheet title.
              - ``max_row``: total rows in the sheet.
              - ``max_column``: total columns (capped at 50 in snapshot).
              - ``rows``: list of rows, each row is a list of cell values.
    """
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        sheets_snapshot: list[dict[str, Any]] = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            max_col = min(ws.max_column or 0, _MAX_COLUMNS)
            max_row_actual = ws.max_row or 0

            rows: list[list[Any]] = []
            for row in ws.iter_rows(max_row=max_rows, max_col=max_col):
                row_values: list[Any] = []
                for cell in row:
                    val = cell.value
                    # Convert unhashable / complex types to string for JSON safety
                    if val is not None and not isinstance(val, (int, float, str, bool)):
                        val = str(val)
                    row_values.append(val)
                rows.append(row_values)

            sheets_snapshot.append(
                {
                    "name": sheet_name,
                    "max_row": max_row_actual,
                    "max_column": max_col,
                    "rows": rows,
                }
            )

        return {
            "filename": filename,
            "sheets": sheets_snapshot,
        }
    finally:
        wb.close()


def group_by_format(file_paths: list[str]) -> dict[str, list[str]]:
    """Group file paths by their naming prefix pattern.

    Files sharing the same alphanumeric prefix (differing only in a trailing
    numeric sequence) are grouped together so the ML service only needs to
    analyze one representative per group.

    Grouping strategy (applied in order):
      1. Try to match ``<prefix>AS-<number>`` pattern.
      2. Fallback: split on the last run of 2+ digits.

    Args:
        file_paths: List of file paths (or basenames) to group.

    Returns:
        Dict mapping group key → list of file paths in that group.

    Examples:
        >>> group_by_format(["SQRT-AS-001.xlsx", "SQRT-AS-002.xlsx"])
        {'SQRT-AS-': ['SQRT-AS-001.xlsx', 'SQRT-AS-002.xlsx']}
    """
    groups: dict[str, list[str]] = defaultdict(list)

    for fp in file_paths:
        basename = os.path.splitext(os.path.basename(fp))[0]
        group_key = _extract_group_key(basename)
        groups[group_key].append(fp)

    return dict(groups)


def _extract_group_key(basename: str) -> str:
    """Derive a grouping key from a filename basename (without extension).

    Tries the ``<prefix>-AS-<digits>`` pattern first (using string search
    for determinism), then falls back to stripping the trailing numeric
    suffix.
    """
    # Try AS-number pattern (deterministic string search)
    as_idx = basename.upper().rfind("-AS-")
    if as_idx >= 0:
        return basename[: as_idx + 4]  # include "-AS-"

    # Fallback: strip trailing digits
    stripped = basename.rstrip("0123456789")
    if stripped and stripped != basename:
        return stripped

    # No numeric suffix — each file is its own group
    return basename
