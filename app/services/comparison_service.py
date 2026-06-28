"""Service for BOM vs Cards comparison and diff.xlsx generation.

This service is part of Workstream D (Aggregation & Packaging).
It does NOT depend on the legacy burlak_parser — all logic is implemented
with Pandas, openpyxl, and xlsxwriter.

Key responsibilities:
  1. Load BOM.xlsx into a Pandas DataFrame (using mapping_config if available).
  2. Aggregate all card_*_materials.json into a Pandas DataFrame.
  3. Compare BOM vs Cards via Pandas merge/join.
  4. Generate diff.xlsx report with discrepancy sheets.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import openpyxl
import pandas as pd

logger = logging.getLogger(__name__)

# ── Column name constants ────────────────────────────────────────────────────

COL_PART_NO = "part_no"
COL_NAME_CN = "name_cn"
COL_NAME_EN = "name_en"
COL_QTY = "qty"
COL_CARD_NUMBER = "card_number"
COL_QTY_BOM = "qty_bom"
COL_QTY_CARDS = "qty_cards"
COL_DIFF = "diff"
COL_TYPE = "type"
COL_FILE = "file"
COL_ERROR = "error"

# ── Discrepancy type labels ──────────────────────────────────────────────────

DISCREPANCY_ONLY_IN_BOM = "Только в BOM"
DISCREPANCY_ONLY_IN_CARDS = "Только в картах"
DISCREPANCY_QTY_MISMATCH = "Конфликт количества"

# ── Chinese header fallback keywords ─────────────────────────────────────────

_PART_NO_KEYWORDS = ("零件号", "零件编号", "part no", "part_no", "part number")
_NAME_CN_KEYWORDS = ("零件名称", "名称", "物料名称", "name_cn")
_NAME_EN_KEYWORDS = ("英文名称", "english name", "name_en")
_QTY_KEYWORDS = ("数量", "用量", "数量/用量", "qty", "quantity")


class ComparisonService:
    """Stateless service for BOM vs Cards comparison."""

    # ── Public API ───────────────────────────────────────────────────────────

    @staticmethod
    def load_bom(
        bom_path: str | Path,
        mapping_config: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Load BOM.xlsx into a DataFrame.

        If *mapping_config* is provided, column indices from
        ``mapping_config["bom"]["sheets"][...]["columns"]`` are used.
        Otherwise a simple header-keyword fallback is applied.

        Returns a DataFrame with columns: ``part_no``, ``name_cn``, ``name_en``, ``qty``.
        """
        bom_path = Path(bom_path)
        if not bom_path.exists():
            raise FileNotFoundError(f"BOM file not found: {bom_path}")

        wb = openpyxl.load_workbook(str(bom_path), data_only=True)

        # Try to use mapping_config first
        bom_sheets_cfg = _resolve_bom_sheets_config(mapping_config)

        if bom_sheets_cfg:
            df = _parse_bom_via_config(wb, bom_sheets_cfg)
        else:
            df = _parse_bom_fallback(wb)

        wb.close()

        if df.empty:
            logger.warning("BOM loaded but resulted in an empty DataFrame")

        return df

    @staticmethod
    def load_cards_data(job_dir: str | Path) -> pd.DataFrame:
        """Aggregate all ``card_*_materials.json`` files from *job_dir*.

        Returns a DataFrame with columns: ``part_no``, ``name_cn``, ``name_en``,
        ``qty``, ``card_number``.  Quantities for the same ``part_no`` appearing
        in multiple cards are summed.
        """
        job_dir = Path(job_dir)
        if not job_dir.is_dir():
            raise NotADirectoryError(f"Job directory not found: {job_dir}")

        records: list[dict[str, Any]] = []
        for fpath in sorted(job_dir.iterdir()):
            if not fpath.name.startswith("card_") or not fpath.name.endswith(
                "_materials.json"
            ):
                continue
            try:
                with open(fpath, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Skipping unreadable card file %s: %s", fpath.name, exc)
                continue

            card_number = data.get("card_number", fpath.stem)
            parts = data.get("parts", [])
            for part in parts:
                records.append(
                    {
                        COL_PART_NO: str(part.get("part_no", "")).strip(),
                        COL_NAME_CN: str(part.get("name_cn", "")).strip(),
                        COL_NAME_EN: str(part.get("name_en", "")).strip(),
                        COL_QTY: float(part.get("qty", 0) or 0),
                        COL_CARD_NUMBER: card_number,
                    }
                )

        if not records:
            logger.warning("No card_*_materials.json found in %s", job_dir)
            return pd.DataFrame(
                columns=[COL_PART_NO, COL_NAME_CN, COL_NAME_EN, COL_QTY, COL_CARD_NUMBER]
            )

        df = pd.DataFrame(records)

        # Aggregate: sum quantities for the same part_no across cards
        agg = (
            df.groupby(COL_PART_NO, as_index=False, sort=False)
            .agg(
                {
                    COL_NAME_CN: "first",
                    COL_NAME_EN: "first",
                    COL_QTY: "sum",
                    COL_CARD_NUMBER: lambda xs: ", ".join(sorted(set(xs))),
                }
            )
            .reset_index(drop=True)
        )
        return agg

    @staticmethod
    def compare(
        bom_df: pd.DataFrame,
        cards_df: pd.DataFrame,
    ) -> dict[str, Any]:
        """Compare BOM and Cards DataFrames.

        Returns a dictionary with keys:
          - ``discrepancies``: DataFrame with columns ``part_no``, ``name_cn``,
            ``name_en``, ``qty_bom``, ``qty_cards``, ``diff``, ``type``.
          - ``summary``: dict with counts.
        """
        # Ensure qty is numeric
        bom_df = bom_df.copy()
        cards_df = cards_df.copy()
        bom_df[COL_QTY] = pd.to_numeric(bom_df[COL_QTY], errors="coerce").fillna(0)
        cards_df[COL_QTY] = pd.to_numeric(cards_df[COL_QTY], errors="coerce").fillna(0)

        merged = pd.merge(
            bom_df[[COL_PART_NO, COL_NAME_CN, COL_NAME_EN, COL_QTY]],
            cards_df[[COL_PART_NO, COL_QTY]],
            on=COL_PART_NO,
            how="outer",
            indicator=True,
            suffixes=("_bom", "_cards"),
        )

        merged.rename(
            columns={
                f"{COL_QTY}_bom": COL_QTY_BOM,
                f"{COL_QTY}_cards": COL_QTY_CARDS,
            },
            inplace=True,
        )

        # Fill NaN quantities with 0
        merged[COL_QTY_BOM] = merged[COL_QTY_BOM].fillna(0)
        merged[COL_QTY_CARDS] = merged[COL_QTY_CARDS].fillna(0)

        # Classify discrepancies
        only_in_bom_mask = merged["_merge"] == "left_only"
        only_in_cards_mask = merged["_merge"] == "right_only"
        qty_mismatch_mask = (~only_in_bom_mask) & (~only_in_cards_mask) & (
            merged[COL_QTY_BOM] != merged[COL_QTY_CARDS]
        )

        merged[COL_TYPE] = ""
        merged.loc[only_in_bom_mask, COL_TYPE] = DISCREPANCY_ONLY_IN_BOM
        merged.loc[only_in_cards_mask, COL_TYPE] = DISCREPANCY_ONLY_IN_CARDS
        merged.loc[qty_mismatch_mask, COL_TYPE] = DISCREPANCY_QTY_MISMATCH

        merged[COL_DIFF] = merged[COL_QTY_CARDS] - merged[COL_QTY_BOM]

        # Filter only discrepancies
        disc_mask = only_in_bom_mask | only_in_cards_mask | qty_mismatch_mask
        discrepancies = merged[disc_mask].copy()

        # Fill missing name info for cards-only rows
        discrepancies[COL_NAME_CN] = discrepancies[COL_NAME_CN].fillna("")
        discrepancies[COL_NAME_EN] = discrepancies[COL_NAME_EN].fillna("")

        # Sort: qty_mismatch first, then only_in_bom, then only_in_cards
        type_order = {
            DISCREPANCY_QTY_MISMATCH: 0,
            DISCREPANCY_ONLY_IN_BOM: 1,
            DISCREPANCY_ONLY_IN_CARDS: 2,
        }
        discrepancies["_sort"] = discrepancies[COL_TYPE].map(type_order).fillna(99)
        discrepancies.sort_values("_sort", inplace=True)
        discrepancies.drop(columns=["_sort", "_merge"], inplace=True)
        discrepancies.reset_index(drop=True, inplace=True)

        # Summary
        total_bom = len(bom_df)
        total_cards = len(cards_df)
        matched = total_bom - int(only_in_bom_mask.sum()) - int(qty_mismatch_mask.sum())

        summary = {
            "total_bom_parts": total_bom,
            "total_cards_parts": total_cards,
            "matched": max(matched, 0),
            "only_in_bom": int(only_in_bom_mask.sum()),
            "only_in_cards": int(only_in_cards_mask.sum()),
            "qty_mismatch": int(qty_mismatch_mask.sum()),
            "total_discrepancies": len(discrepancies),
        }

        return {"discrepancies": discrepancies, "summary": summary}

    @staticmethod
    def generate_report(
        comparison_result: dict[str, Any],
        output_path: str | Path,
        failed_cards: list[dict[str, str]] | None = None,
    ) -> str:
        """Generate ``diff.xlsx`` from the comparison result.

        Args:
            comparison_result: Output of :meth:`compare`.
            output_path: Where to write the XLSX file.
            failed_cards: Optional list of dicts with keys ``card_path`` /
                ``file`` and ``error_message`` / ``error``.

        Returns:
            The *output_path* as a string.
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        discrepancies: pd.DataFrame = comparison_result.get("discrepancies", pd.DataFrame())
        summary: dict[str, int] = comparison_result.get("summary", {})

        import xlsxwriter

        workbook = xlsxwriter.Workbook(str(output_path))

        # ── Formats ──────────────────────────────────────────────────────────
        header_fmt = workbook.add_format(
            {
                "bold": True,
                "bg_color": "#4472C4",
                "font_color": "white",
                "border": 1,
                "text_wrap": True,
                "align": "center",
                "valign": "vcenter",
                "font_size": 11,
            }
        )
        cell_fmt = workbook.add_format(
            {
                "border": 1,
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        )
        cell_center_fmt = workbook.add_format(
            {
                "border": 1,
                "text_wrap": True,
                "align": "center",
                "valign": "vcenter",
                "font_size": 10,
            }
        )
        cell_num_fmt = workbook.add_format(
            {
                "border": 1,
                "align": "center",
                "valign": "vcenter",
                "num_format": "0.00",
                "font_size": 10,
            }
        )
        title_fmt = workbook.add_format(
            {
                "bold": True,
                "font_size": 14,
                "font_color": "#1F3864",
            }
        )
        label_fmt = workbook.add_format(
            {
                "bold": True,
                "font_size": 11,
            }
        )
        value_fmt = workbook.add_format(
            {
                "font_size": 11,
            }
        )

        # Color formats per discrepancy type
        qty_fmt = workbook.add_format(
            {
                "border": 1,
                "bg_color": "#FCE4EC",
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        )
        bom_only_fmt = workbook.add_format(
            {
                "border": 1,
                "bg_color": "#FFF2CC",
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        )
        cards_only_fmt = workbook.add_format(
            {
                "border": 1,
                "bg_color": "#D9E2F3",
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        )

        # ══════════════════════════════════════════════════════════════════════
        # Sheet 1: Summary
        # ══════════════════════════════════════════════════════════════════════
        ws_summary = workbook.add_worksheet("Сводка")
        ws_summary.set_tab_color("#1F3864")
        ws_summary.hide_gridlines(2)
        ws_summary.set_column(0, 0, 30)
        ws_summary.set_column(1, 1, 15)
        ws_summary.set_column(2, 2, 15)

        ws_summary.merge_range("A1:C1", "СВОДКА РЕЗУЛЬТАТОВ СВЕРКИ", title_fmt)
        ws_summary.set_row(0, 25)

        row = 2
        for label, key in [
            ("Деталей в BOM", "total_bom_parts"),
            ("Деталей в картах", "total_cards_parts"),
            ("Совпало", "matched"),
            ("Только в BOM", "only_in_bom"),
            ("Только в картах", "only_in_cards"),
            ("Конфликт количества", "qty_mismatch"),
            ("Всего расхождений", "total_discrepancies"),
        ]:
            ws_summary.write(row, 0, label, label_fmt)
            ws_summary.write(row, 1, summary.get(key, 0), value_fmt)
            row += 1

        # ══════════════════════════════════════════════════════════════════════
        # Sheet 2: All discrepancies
        # ══════════════════════════════════════════════════════════════════════
        if not discrepancies.empty:
            _write_discrepancies_sheet(workbook, "Расхождения", discrepancies)

            # Split by type into separate sheets
            for dtype, sheet_name in [
                (DISCREPANCY_ONLY_IN_BOM, "Только в BOM"),
                (DISCREPANCY_ONLY_IN_CARDS, "Только в картах"),
                (DISCREPANCY_QTY_MISMATCH, "Конфликт количества"),
            ]:
                subset = discrepancies[discrepancies[COL_TYPE] == dtype]
                if not subset.empty:
                    _write_discrepancies_sheet(workbook, sheet_name, subset)

        # ══════════════════════════════════════════════════════════════════════
        # Sheet: Failed cards
        # ══════════════════════════════════════════════════════════════════════
        if failed_cards:
            ws_err = workbook.add_worksheet("Ошибки файлов")
            ws_err.set_tab_color("#C00000")
            ws_err.freeze_panes(1, 0)
            ws_err.set_column(0, 0, 50)
            ws_err.set_column(1, 1, 70)

            err_headers = ["Имя файла", "Ошибка"]
            for ci, h in enumerate(err_headers):
                ws_err.write(0, ci, h, header_fmt)
            ws_err.set_row(0, 30)

            for ri, card in enumerate(failed_cards, 1):
                fname = card.get("card_path") or card.get("file") or ""
                err_msg = card.get("error_message") or card.get("error") or ""
                ws_err.write(ri, 0, fname, cell_fmt)
                ws_err.write(ri, 1, err_msg, cell_fmt)

        workbook.close()
        logger.info("Report saved to %s", output_path)
        return str(output_path)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _resolve_bom_sheets_config(
    mapping_config: dict[str, Any] | None,
) -> list[dict[str, Any]] | None:
    """Extract BOM sheet configs from mapping_config, or return None."""
    if not mapping_config:
        return None
    try:
        sheets = mapping_config["bom"]["sheets"]
        # Filter only sheets with type "bom_data"
        bom_sheets = [s for s in sheets if s.get("sheet_type") == "bom_data"]
        return bom_sheets if bom_sheets else None
    except (KeyError, TypeError):
        return None


def _parse_bom_via_config(
    wb: openpyxl.Workbook,
    sheet_configs: list[dict[str, Any]],
) -> pd.DataFrame:
    """Parse BOM using column indices from mapping_config."""
    all_rows: list[dict[str, Any]] = []

    for cfg in sheet_configs:
        sheet_name = cfg.get("sheet_name")
        if sheet_name is None or sheet_name not in wb.sheetnames:
            continue
        ws = wb[sheet_name]

        columns_cfg = cfg.get("columns", {})
        part_no_col = columns_cfg.get("part_no", {}).get("col_index", 0)
        name_cn_col = columns_cfg.get("name_cn", {}).get("col_index", 0)
        name_en_col = columns_cfg.get("name_en", {}).get("col_index", 0)
        qty_col = columns_cfg.get("qty", {}).get("col_index", 0)

        data_start = cfg.get("data_start_row", 2)
        max_row = ws.max_row or data_start

        for row_idx in range(data_start, max_row + 1):
            pn = _cell_str(ws, row_idx, part_no_col)
            if not pn:
                continue
            all_rows.append(
                {
                    COL_PART_NO: pn,
                    COL_NAME_CN: _cell_str(ws, row_idx, name_cn_col),
                    COL_NAME_EN: _cell_str(ws, row_idx, name_en_col),
                    COL_QTY: _cell_float(ws, row_idx, qty_col),
                }
            )

    return pd.DataFrame(all_rows)


def _parse_bom_fallback(wb: openpyxl.Workbook) -> pd.DataFrame:
    """Fallback: scan all sheets for known Chinese/English headers."""
    all_rows: list[dict[str, Any]] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        if ws.max_row is None or ws.max_row < 2:
            continue

        # Scan first 10 rows for header keywords
        header_row_idx = None
        col_map: dict[str, int] = {}

        for row_idx in range(1, min(ws.max_row + 1, 11)):
            for col_idx in range(1, min((ws.max_column or 10) + 1, 10)):
                val = _cell_str(ws, row_idx, col_idx).lower()
                if not val:
                    continue
                if _matches_any(val, _PART_NO_KEYWORDS):
                    col_map[COL_PART_NO] = col_idx
                elif _matches_any(val, _NAME_CN_KEYWORDS):
                    col_map[COL_NAME_CN] = col_idx
                elif _matches_any(val, _NAME_EN_KEYWORDS):
                    col_map[COL_NAME_EN] = col_idx
                elif _matches_any(val, _QTY_KEYWORDS):
                    col_map[COL_QTY] = col_idx

            if COL_PART_NO in col_map:
                header_row_idx = row_idx
                break

        if header_row_idx is None:
            continue

        data_start = header_row_idx + 1
        part_no_col = col_map.get(COL_PART_NO, 0)
        name_cn_col = col_map.get(COL_NAME_CN, 0)
        name_en_col = col_map.get(COL_NAME_EN, 0)
        qty_col = col_map.get(COL_QTY, 0)

        for row_idx in range(data_start, ws.max_row + 1):
            pn = _cell_str(ws, row_idx, part_no_col)
            if not pn:
                continue
            all_rows.append(
                {
                    COL_PART_NO: pn,
                    COL_NAME_CN: _cell_str(ws, row_idx, name_cn_col),
                    COL_NAME_EN: _cell_str(ws, row_idx, name_en_col),
                    COL_QTY: _cell_float(ws, row_idx, qty_col),
                }
            )

    return pd.DataFrame(all_rows)


def _write_discrepancies_sheet(
    workbook: Any,  # xlsxwriter.Workbook
    sheet_name: str,
    df: pd.DataFrame,
) -> None:
    """Write a discrepancies DataFrame into a named worksheet."""
    ws = workbook.add_worksheet(sheet_name)
    ws.freeze_panes(1, 0)

    headers = [
        "Каталожный номер",
        "Название (кит.)",
        "Название (англ.)",
        "Кол-во в BOM",
        "Кол-во в картах",
        "Разница",
        "Тип",
    ]
    widths = [22, 30, 30, 14, 14, 14, 30]

    header_fmt = workbook.add_format(
        {
            "bold": True,
            "bg_color": "#4472C4",
            "font_color": "white",
            "border": 1,
            "text_wrap": True,
            "align": "center",
            "valign": "vcenter",
            "font_size": 11,
        }
    )
    cell_fmt = workbook.add_format(
        {
            "border": 1,
            "text_wrap": True,
            "valign": "vcenter",
            "font_size": 10,
        }
    )
    cell_num_fmt = workbook.add_format(
        {
            "border": 1,
            "align": "center",
            "valign": "vcenter",
            "num_format": "0.00",
            "font_size": 10,
        }
    )

    for ci, (h, w) in enumerate(zip(headers, widths)):
        ws.set_column(ci, ci, w)
        ws.write(0, ci, h, header_fmt)
    ws.set_row(0, 30)

    # Color mapping
    color_map = {
        DISCREPANCY_QTY_MISMATCH: workbook.add_format(
            {
                "border": 1,
                "bg_color": "#FCE4EC",
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        ),
        DISCREPANCY_ONLY_IN_BOM: workbook.add_format(
            {
                "border": 1,
                "bg_color": "#FFF2CC",
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        ),
        DISCREPANCY_ONLY_IN_CARDS: workbook.add_format(
            {
                "border": 1,
                "bg_color": "#D9E2F3",
                "text_wrap": True,
                "valign": "vcenter",
                "font_size": 10,
            }
        ),
    }

    for ri, (_, row) in enumerate(df.iterrows(), 1):
        dtype = row.get(COL_TYPE, "")
        fmt = color_map.get(dtype, cell_fmt)
        ws.write(ri, 0, row.get(COL_PART_NO, ""), fmt)
        ws.write(ri, 1, row.get(COL_NAME_CN, ""), fmt)
        ws.write(ri, 2, row.get(COL_NAME_EN, ""), fmt)
        ws.write(ri, 3, float(row.get(COL_QTY_BOM, 0)), cell_num_fmt)
        ws.write(ri, 4, float(row.get(COL_QTY_CARDS, 0)), cell_num_fmt)
        ws.write(ri, 5, float(row.get(COL_DIFF, 0)), cell_num_fmt)
        ws.write(ri, 6, dtype, fmt)


# ── Cell helpers ─────────────────────────────────────────────────────────────


def _cell_str(ws: Any, row: int, col: int) -> str:
    """Read a cell value and return as stripped string."""
    if col < 1:
        return ""
    val = ws.cell(row=row, column=col).value
    if val is None:
        return ""
    return str(val).strip()


def _cell_float(ws: Any, row: int, col: int) -> float:
    """Read a cell value and return as float (0 if empty/invalid)."""
    if col < 1:
        return 0.0
    val = ws.cell(row=row, column=col).value
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    """Check if *text* contains any of the *keywords* (case-insensitive)."""
    return any(kw in text for kw in keywords)