"""Module for splitting multi-sheet Excel files into individual single-sheet files.

Uses the "remove the extra" method (not "copy the needed"):
  1. Loads the source .xlsx as a ZIP archive of XML.
  2. Creates a copy of the entire Workbook for each sheet.
  3. Removes all sheets from the copy except the target.
  4. Cleans global named ranges (defined names / named ranges),
     referencing removed sheets — this eliminates the Excel error
     "Removed Feature: Named range from /xl/workbook.xml part (Workbook)".

Method advantages:
  - 100% preservation of formatting, styles, images, fonts.
  - Column widths, row heights, and merged cells are preserved.
  - Images, charts, and frozen panes are preserved.
  - No "Named range" error on open.

Supports parallelism via ThreadPoolExecutor.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

try:
    from lxml import etree as _lxml_etree

    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed


class ExcelSheet:
    """Wrapper over an Excel sheet for a unified openpyxl / xlrd API."""

    def __init__(self, ws: Any, engine: str):
        self._ws = ws
        self._engine = engine

    @property
    def max_row(self) -> int:
        if self._engine == "openpyxl":
            return self._ws.max_row or 0
        else:
            return self._ws.nrows

    @property
    def max_column(self) -> int:
        if self._engine == "openpyxl":
            return self._ws.max_column or 0
        else:
            return self._ws.ncols

    def cell_value(self, row: int, column: int) -> Any:
        try:
            if self._engine == "openpyxl":
                return self._ws.cell(row=row, column=column).value
            else:
                val = self._ws.cell_value(row - 1, column - 1)
                if val == "" or val is None:
                    return None
                if isinstance(val, float) and val == int(val):
                    return int(val)
                return val
        except Exception:
            return None


class ExcelReader:
    """Universal Excel file reader.
    Supports .xlsx (openpyxl) and .xls (xlrd).
    Uses openpyxl for .xlsx, xlrd for .xls.
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._wb: Any = None
        self._engine: str = ""
        self._sheet_names: list[str] = []
        self._sheets: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        ext = os.path.splitext(self.file_path)[1].lower()

        if ext == ".xls":
            self._load_via_xlrd()
        else:
            try:
                import openpyxl

                wb = openpyxl.load_workbook(
                    self.file_path,
                    data_only=True,
                )
                self._engine = "openpyxl"
                self._wb = wb
                self._sheet_names = list(wb.sheetnames)
                for sn in self._sheet_names:
                    self._sheets[sn] = wb[sn]
                return
            except Exception:
                pass
            # Fallback: read_only mode (handles WPS/slightly corrupted files)
            try:
                import openpyxl

                wb = openpyxl.load_workbook(
                    self.file_path,
                    data_only=True,
                    read_only=True,
                )
                self._engine = "openpyxl"
                self._wb = wb
                self._sheet_names = list(wb.sheetnames)
                for sn in self._sheet_names:
                    self._sheets[sn] = wb[sn]
                return
            except Exception:
                pass
            self._load_via_xlrd()

    def _load_via_xlrd(self) -> None:
        try:
            import xlrd
        except ImportError:
            raise ImportError(
                "xlrd is required to read .xls files. Install: pip install xlrd"
            )

        try:
            wb = xlrd.open_workbook(self.file_path)
            self._engine = "xlrd"
            self._wb = wb
            self._sheet_names = list(wb.sheet_names())
            for sn in self._sheet_names:
                self._sheets[sn] = wb.sheet_by_name(sn)
        except Exception as e:
            raise ValueError(f"Failed to open Excel file {self.file_path}: {e}")

    @property
    def sheet_names(self) -> list[str]:
        return self._sheet_names

    def get_sheet(self, name: str) -> ExcelSheet:
        if name not in self._sheets:
            raise KeyError(f"Sheet '{name}' not found")
        return ExcelSheet(self._sheets[name], self._engine)

    def close(self) -> None:
        if self._engine == "openpyxl" and self._wb is not None:
            self._wb.close()


try:
    from openpyxl.utils.cell import get_column_letter, range_boundaries
except ImportError:
    # Fallback implementations if openpyxl is not installed
    import string

    def get_column_letter(col_idx: int) -> str:
        """Convert column index to Excel column letter (A=1, B=2, ...)."""
        result = ""
        while col_idx > 0:
            col_idx, remainder = divmod(col_idx - 1, 26)
            result = string.ascii_uppercase[remainder] + result
        return result

    def range_boundaries(range_string: str) -> tuple:
        """Parse Excel range string like 'A1:B10' into (min_col, min_row, max_col, max_row)."""
        if ":" not in range_string:
            range_string = f"{range_string}:{range_string}"
        start, end = range_string.split(":", 1)
        start_col, start_row = _split_cell_ref(start.strip())
        end_col, end_row = _split_cell_ref(end.strip())
        return (start_col, start_row, end_col, end_row)

    def _split_cell_ref(ref: str) -> tuple:
        """Split cell reference like 'A1' into (col_index, row_number)."""
        match = re.match(r"^([A-Za-z]+)(\d+)$", ref.strip())
        if not match:
            raise ValueError(f"Invalid cell reference: {ref}")
        col_str, row_str = match.groups()
        col = 0
        for ch in col_str.upper():
            col = col * 26 + (ord(ch) - ord("A") + 1)
        return (col, int(row_str))


# Suppress openpyxl warnings about DrawingML (incomplete support)
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

logger = logging.getLogger(__name__)

# Characters forbidden in Windows/Linux filenames
_ILLEGAL_FS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Decorative Unicode characters to strip from filenames
# (stars, diamonds, circles, arrows, etc.)
_DECORATIVE_CHARS_RE = re.compile(r'[☆★●○◆◇■□▲△▼▽♠♣♥♦↗→←↑↓«»""' "„]")

# Multiple underscores/dots/spaces → single
_MULTI_SEP_RE = re.compile(r"[_ .]{2,}")

# Regex for cell reference: "A3390" → groups ("A", "3390")
_CELL_REF_RE = re.compile(r"^([A-Z]+)(\d+)$")

# Excel OOXML namespaces
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_PKG_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
NS_DRAWINGML = "http://schemas.openxmlformats.org/drawingml/2006/main"
VML_NS = "urn:schemas-microsoft-com:vml"
OFFICE_NS = "urn:schemas-microsoft-com:office:office"

# Register namespaces globally
# Must register ALL namespaces that may appear
# in OOXML files to avoid ns0:/ns1: prefixes.
# _serialize_xml() dynamically switches the default namespace on each call.
ET.register_namespace("", NS_MAIN)
ET.register_namespace("r", NS_R)
ET.register_namespace("xdr", NS_DRAWING)
ET.register_namespace("a", NS_DRAWINGML)
ET.register_namespace("ct", NS_CT)

# lxml namespace map for proper OOXML serialization
_LXML_NS = {
    "xdr": NS_DRAWING,
    "a": NS_DRAWINGML,
    "r": NS_R,
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "x14": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main",
    "x14ac": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac",
    "x15": "http://schemas.microsoft.com/office/spreadsheetml/2010/11/main",
    "xm": "http://schemas.microsoft.com/office/excel/2006/main",
    "rel": NS_PKG_RELS,
    "ct": NS_CT,
    "v": VML_NS,
    "o": OFFICE_NS,
    "x": "urn:schemas-microsoft-com:office:excel",
    "pr": "http://schemas.microsoft.com/office/2006/relationships",
}


def _lxml_to_bytes(root, xml_declaration: bool = True) -> bytes:
    """Serialize lxml element to bytes with MS Excel-compatible declaration.

    Uses lxml.etree.tostring with proper encoding and standalone declaration.
    Falls back to ElementTree if lxml is unavailable.
    """
    if _HAS_LXML:
        return _lxml_etree.tostring(
            root,
            xml_declaration=xml_declaration,
            encoding="UTF-8",
            standalone=True,
        )
    return _serialize_xml(root, NS_MAIN)


def _serialize_xml(
    root: ET.Element, default_ns_uri: str, extra_ns: dict[str, str] | None = None
) -> bytes:
    """Serialize an ET.Element with a specific default namespace.

    Saves and restores the global ET._namespace_map to avoid corruption.
    Used because different OOXML files require different default namespaces:
      - sheet XML:  NS_MAIN as default
      - Content_Types:  NS_CT as default
      - rels files:  NS_PKG_RELS as default

    CRITICAL: Uses custom XML declaration with standalone="yes", double quotes,
    and Windows-style \r\n line endings. MS Excel requires these for compatibility.
    Python 3.14: ET.tostring(standalone=True) raises TypeError, so we work around it.
    """
    old_default_uri = None
    for uri_key, prefix_val in ET._namespace_map.items():
        if prefix_val == "":
            old_default_uri = uri_key
            break
    old_extras: dict[str, str | None] = {}
    try:
        ET.register_namespace("", default_ns_uri)
        if extra_ns:
            for p, uri in extra_ns.items():
                old_extras[p] = ET._namespace_map.get(uri)
                ET.register_namespace(p, uri)
        # Serialize without declaration, then prepend MS Excel-compatible declaration
        body = ET.tostring(root, xml_declaration=False, encoding="UTF-8")
        declaration = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        return declaration + body
    finally:
        try:
            ET.register_namespace("", old_default_uri)
        except TypeError:
            pass
        if extra_ns:
            for p, uri in extra_ns.items():
                old_prefix = old_extras.get(p)
                if old_prefix is not None:
                    ET._namespace_map[uri] = old_prefix
                else:
                    ET._namespace_map.pop(uri, None)


# ─── Types for vertical split ──────────────────────────────────────


@dataclass
class TableBoundary:
    """Boundaries of a single table (operation) within a sheet."""

    header_row: int  # Table header row
    data_start: int  # First data row (header_row + 1)
    data_end: int  # Last data row
    operation_name: str = ""  # Operation name
    source_path: str = ""
    sheet_name: str = ""
    card_label: str = ""


@dataclass
class SplitStatistics:
    """Sheet splitting statistics."""

    openpyxl_fallback_count: int = 0
    openpyxl_fallback_files: list[str] = field(default_factory=list)
    copy_fallback_count: int = 0
    copy_fallback_files: list[str] = field(default_factory=list)


class CardSplitter:
    """Service for splitting multi-sheet operational cards into individual files."""

    def __init__(self, max_workers: int | None = None):
        """Initialise the splitter.

        Args:
            max_workers: Maximum number of processes for parallel splitting.
                         Default: CPU count.
        """
        self.max_workers = max_workers or os.cpu_count() or 4
        self.openpyxl_fallback_count = 0
        self.openpyxl_fallback_files: set[str] = set()
        self.copy_fallback_count = 0
        self.copy_fallback_files: set[str] = set()
        self.manifest: dict[str, list[str]] = {}

    def split_file(
        self,
        source_path: str,
        output_dir: str,
        sheet_names: list[str],
        file_label: str = "",
    ) -> list[str]:
        """Split a single .xlsx file into multiple single-sheet files.

        For .xls files (legacy) — copies as-is without splitting.

        Args:
            source_path: Path to the source .xlsx/.xls file.
            output_dir: Output directory.
            sheet_names: Sheet names to extract.
            file_label: File label for output file naming.

        Returns:
            List of created file paths.
        """
        os.makedirs(output_dir, exist_ok=True)
        created: list[str] = []
        original_name = os.path.basename(source_path)

        ext_lower = os.path.splitext(source_path)[1].lower()

        if ext_lower == ".xls":
            safe_label = _safe_filename(file_label)[:50] if file_label else ""
            basename = os.path.splitext(os.path.basename(source_path))[0]
            out_name = (
                f"{safe_label}_{basename}.xls" if safe_label else f"{basename}.xls"
            )
            output_path = os.path.join(output_dir, out_name)
            counter = 1
            while True:
                try:
                    fd = os.open(output_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    break
                except FileExistsError:
                    base, ext = os.path.splitext(out_name)
                    output_path = os.path.join(output_dir, f"{base}_{counter}{ext}")
                    counter += 1
            shutil.copy2(source_path, output_path)
            created.append(output_path)
            self.manifest.setdefault(original_name, []).append(
                os.path.basename(output_path)
            )
            logger.info(
                "Copied .xls file (without splitting): %s",
                os.path.basename(source_path),
            )
            return created

        if ext_lower != ".xlsx":
            logger.debug("Skipping non-.xlsx/.xls file: %s", source_path)
            return created

        for sheet_name in sheet_names:
            safe_label = _safe_filename(file_label)[:50] if file_label else ""
            safe_sheet = _safe_filename(sheet_name)[:50]
            if safe_label:
                output_filename = f"{safe_label}_{safe_sheet}.xlsx"
            else:
                output_filename = f"{safe_sheet}.xlsx"

            output_path = os.path.join(output_dir, output_filename)

            # Skip if path already exists (pre-allocated by main thread)
            if os.path.exists(output_path):
                continue

            try:
                self._extract_sheet(source_path, output_path, sheet_name)
                created.append(output_path)
                self.manifest.setdefault(original_name, []).append(
                    os.path.basename(output_path)
                )
                logger.debug("Created: %s", os.path.basename(output_path))
            except Exception as e:
                logger.warning(
                    "Error splitting sheet '%s' from %s: %s",
                    sheet_name,
                    os.path.basename(source_path),
                    e,
                )

        return created

    def split_many_parallel(
        self,
        tasks: list[tuple[str, str, list[str], str]],
    ) -> tuple[list[str], list[tuple[str, str]], int, list[str], dict[str, list[str]]]:
        """Split multiple files in parallel.

        Guarantees deterministic order: results are sorted
        by full path for reproducibility.

        Args:
            tasks: List of tuples (source_path, output_dir, sheet_names, file_label).

        Returns:
            Tuple (all_created_files, errors, openpyxl_fallback_count,
                    openpyxl_fallback_files, manifest).
        """
        all_created: list[str] = []
        errors: list[tuple[str, str]] = []
        all_openpyxl_count = 0
        all_openpyxl_files: list[str] = []
        merged_manifest: dict[str, list[str]] = {}

        # Pre-compute all paths deterministically (synchronous, main thread)
        path_map = preallocate_split_paths(tasks, tasks[0][1] if tasks else "")

        # Collect flat tasks (source_path, output_path, sheet_name)
        sheet_tasks: list[tuple[str, str, str]] = []
        for source_path, out_dir, sheet_names, file_label in tasks:
            for sheet_name in sheet_names:
                output_path = path_map.get((source_path, sheet_name))
                if output_path:
                    sheet_tasks.append((source_path, output_path, sheet_name))

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for src, out, sheet in sheet_tasks:
                future = executor.submit(
                    _extract_to_path_worker,
                    src,
                    out,
                    sheet,
                )
                futures[future] = (src, out, sheet)

            for future in as_completed(futures):
                src, out, sheet = futures[future]
                try:
                    worker_result = future.result()
                    result_path = worker_result.get("path")
                    err_msg = worker_result.get("error")
                    source_basename = worker_result.get("source_basename", "")

                    if result_path:
                        all_created.append(result_path)
                        if worker_result.get("used_fallback"):
                            all_openpyxl_count += 1
                            if source_basename:
                                all_openpyxl_files.append(source_basename)
                        if source_basename:
                            merged_manifest.setdefault(source_basename, []).append(
                                os.path.basename(result_path),
                            )
                    else:
                        logger.error(
                            "Split error %s: %s",
                            os.path.basename(src),
                            err_msg,
                        )
                        errors.append((src, err_msg or "Unknown error"))
                except Exception as e:
                    err_msg = str(e)
                    logger.error(
                        "Critical error in parallel splitting %s: %s",
                        os.path.basename(src),
                        err_msg,
                    )
                    errors.append((src, err_msg))

        # Sort for deterministic order
        all_created.sort()
        errors.sort(key=lambda x: x[0])
        all_openpyxl_files.sort()

        # Sort values in the manifest
        for orig in merged_manifest:
            merged_manifest[orig].sort()

        return (
            all_created,
            errors,
            all_openpyxl_count,
            all_openpyxl_files,
            merged_manifest,
        )

    def _extract_sheet(
        self,
        source_path: str,
        output_path: str,
        keep_sheet_name: str,
    ) -> None:
        """Extract a single sheet from an .xlsx file.

        LINEAR strategy (no recursion), performance priority:
          1. ZIP method: fast (milliseconds), handles 90%+ of files.
          2. On ZIP error → ONE openpyxl attempt (slow, for WPS/corrupted).
          3. On openpyxl error → exception.

        Args:
            source_path: Path to the source .xlsx file.
            output_path: Path for the new .xlsx file.
            keep_sheet_name: Name of the sheet to keep.

        Raises:
            ValueError: If the target sheet is not found.
            Exception: If both methods failed.
        """
        # Attempt 1: ZIP (fast — milliseconds per file)
        try:
            self._extract_sheet_via_zip(source_path, output_path, keep_sheet_name)
            if not _validate_split_file(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
                raise ValueError(f"Invalid split output: {output_path}")
            return
        except Exception as e:
            logger.warning(
                "ZIP method could not split %s: %s. Trying openpyxl...",
                os.path.basename(source_path),
                e,
            )

        # Attempt 2: openpyxl (slow — for WPS/corrupted files, one attempt)
        try:
            self._extract_sheet_via_openpyxl(source_path, output_path, keep_sheet_name)
            self.openpyxl_fallback_count += 1
            self.openpyxl_fallback_files.add(os.path.basename(source_path))
            logger.info(
                "openpyxl successfully split sheet: %s in file %s",
                keep_sheet_name,
                os.path.basename(source_path),
            )
            return
        except Exception as openpyxl_e:
            openpyxl_error = openpyxl_e
            logger.warning(
                "openpyxl could not split sheet '%s' from %s: %s. "
                "Trying to copy source file...",
                keep_sheet_name,
                os.path.basename(source_path),
                openpyxl_e,
            )

        # Attempt 3: Copy source file as-is (last resort)
        # If the file is valid and opens but cannot be split —
        # copy it as-is. Better to get an unsplit file
        # than lose data by sending it to corrupted_cards.
        try:
            _copy_source_as_fallback(source_path, output_path)
            self.copy_fallback_count += 1
            self.copy_fallback_files.add(os.path.basename(source_path))
            logger.info(
                "Source file copied (copy fallback) for sheet '%s' from %s "
                "(may contain multiple sheets in output file)",
                keep_sheet_name,
                os.path.basename(source_path),
            )
            return
        except Exception as copy_e:
            logger.error(
                "All three methods of splitting sheet '%s' from %s failed. "
                "ZIP: see above. openpyxl: %s. Copy: %s",
                keep_sheet_name,
                os.path.basename(source_path),
                openpyxl_error,
                copy_e,
            )
            raise

    def _extract_sheet_via_openpyxl(
        self,
        source_path: str,
        output_path: str,
        keep_sheet_name: str,
    ) -> None:
        """Extract one sheet via openpyxl (load → remove sheets → save).

        This method correctly handles files created by WPS Office
        and other OOXML generators that may contain
        non-standard CRC checksums or corrupted ZIP entries.

        Includes workaround for WPS bug: DefinedNameDict without definedName attribute.

        Args:
            source_path: Path to the source .xlsx file.
            output_path: Path for the new .xlsx file.
            keep_sheet_name: Name of the sheet to keep.

        Raises:
            ValueError: If the target sheet is not found.
            Exception: On openpyxl load/save error.
        """
        import openpyxl

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            wb = openpyxl.load_workbook(source_path)
        sheet_names = wb.sheetnames

        if keep_sheet_name not in sheet_names:
            wb.close()
            raise ValueError(f"Sheet '{keep_sheet_name}' not found in file")

        if len(sheet_names) <= 1:
            wb.save(output_path)
            wb.close()
            return

        sheets_to_remove = [n for n in sheet_names if n != keep_sheet_name]

        # ── WPS BUG FIX: Patch DefinedNameDict before sheet removal ──
        # WPS Office produces corrupted OOXML where wb.defined_names
        # lacks the definedName attribute. openpyxl crashes on del wb[sheet]
        # with AttributeError: 'DefinedNameDict' object has no attribute 'definedName'.
        # Solution: forcefully create empty attributes.
        dn = getattr(wb, "defined_names", None)
        if dn is not None:
            if not hasattr(dn, "definedName"):
                dn.definedName = []
            if not hasattr(dn, "elements"):
                dn.elements = []

        # Remove named ranges referencing removed sheets
        try:
            if dn is not None and dn.definedName:
                to_delete = []
                for defined_name in dn.definedName:
                    attr_text = getattr(defined_name, "attr_text", None) or str(
                        defined_name
                    )
                    for deleted in sheets_to_remove:
                        if deleted in attr_text or f"'{deleted}'" in attr_text:
                            to_delete.append(defined_name)
                            break
                for defined_name in to_delete:
                    try:
                        dn.definedName.remove(defined_name)
                    except Exception as e:
                        logger.debug("Failed to remove defined name: %s", e)
        except Exception as e:
            logger.debug("Defined name cleanup failed (non-critical): %s", e)

        for name in sheets_to_remove:
            try:
                del wb[name]
            except Exception as e:
                logger.debug("Failed to remove sheet %s: %s", name, e)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            wb.save(output_path)
        wb.close()

    def _extract_sheet_via_zip(
        self,
        source_path: str,
        output_path: str,
        keep_sheet_name: str,
    ) -> None:
        """Extract one sheet via pure ZIP manipulation.

        ALGORITHM (KEY):
        "Keep only what's needed" strategy:
          1. Read the original ZIP into memory.
          2. Find the rId and path to the target sheet.
          3. Recursively trace all .rels from the sheet → find all needed files
             (drawing XML, VML, OLE, images, printerSettings, their .rels).
          4. Add mandatory files: workbook, styles, theme, sharedStrings, docProps.
          5. Create NEW workbook.xml: only 1 sheet + cleaned definedNames.
             IMPORTANT: all other elements (fileVersion, workbookPr, bookViews,
             calcPr, AlternateContent) are copied from the original AS-IS.
          6. Create NEW workbook.xml.rels: only the sheet + shared items.
          7. Filter Content_Types.xml: only Override for existing files.
          8. Write the new ZIP.

        IMPORTANT: NO openpyxl, NO file renaming!
        All original XML files and binary data are copied AS-IS
        with their original names (sheet8.xml stays sheet8.xml).
        This guarantees 100% preservation of all references inside drawing,
        VML, OLE and other files.

        Args:
            source_path: Path to the source .xlsx file.
            output_path: Path for the new .xlsx file.
            keep_sheet_name: Name of the sheet to keep.

        Raises:
            ValueError: If the target sheet is not found.
        """
        # ── PHASE 1: Read the original ZIP ──
        # Try several encodings for filenames in ZIP.
        # Chinese Windows systems create ZIP with GBK-encoded names,
        # but Python zipfile defaults to CP437, which breaks
        # file paths (mojibake) and makes name-based lookup impossible.
        with open(source_path, "rb") as f:
            zip_data = f.read()

        orig_entries: dict[str, bytes] = {}
        _zip_loaded = False
        for _enc in (None, "gbk", "utf-8", "cp1251", "latin-1"):
            try:
                kwargs = {"metadata_encoding": _enc} if _enc else {}
                with zipfile.ZipFile(io.BytesIO(zip_data), "r", **kwargs) as zf:
                    for name in zf.namelist():
                        try:
                            orig_entries[name] = zf.read(name)
                        except (zipfile.BadZipFile, Exception):
                            pass
                _zip_loaded = True
                break
            except (zipfile.BadZipFile, UnicodeDecodeError):
                continue
        if not _zip_loaded:
            raise ValueError(f"Cannot read source ZIP with any encoding: {source_path}")

        # ── PHASE 2: Find the sheet in workbook.xml ──
        wb_xml = orig_entries.get("xl/workbook.xml")
        if wb_xml is None:
            raise ValueError("xl/workbook.xml not found")

        wb_root = ET.fromstring(wb_xml)
        sheets_elem = wb_root.find(f"{{{NS_MAIN}}}sheets")
        if sheets_elem is None:
            raise ValueError("<sheets> not found in original workbook.xml")

        # Find the target sheet rId
        target_r_id: str | None = None
        for sheet_el in sheets_elem.findall(f"{{{NS_MAIN}}}sheet"):
            if sheet_el.get("name") == keep_sheet_name:
                target_r_id = sheet_el.get(f"{{{NS_R}}}id") or sheet_el.get("r:id")
                break

        if target_r_id is None:
            raise ValueError(f"Sheet '{keep_sheet_name}' not found")

        # ── PHASE 3a: Find the sheet path from workbook.xml.rels ──
        rels_xml = orig_entries.get("xl/_rels/workbook.xml.rels")
        if rels_xml is None:
            raise ValueError("xl/_rels/workbook.xml.rels not found")

        rels_root = ET.fromstring(rels_xml)
        orig_sheet_path = ""
        for rel_el in rels_root:
            if rel_el.get("Id") == target_r_id:
                orig_sheet_path = rel_el.get("Target", "")
                break

        if not orig_sheet_path:
            raise ValueError(f"No target for rId {target_r_id}")

        # Normalize path
        orig_sheet_path = orig_sheet_path.lstrip("/")
        if not orig_sheet_path.startswith("xl/"):
            orig_sheet_path = "xl/" + orig_sheet_path

        # ── PHASE 3b: Recursively trace all .rels ──
        needed: set[str] = set()

        def _trace_rels(rels_path: str, base_dir: str) -> None:
            """Recursively trace .rels, adding all found files."""
            if rels_path not in orig_entries:
                return
            try:
                tr_root = ET.fromstring(orig_entries[rels_path])
                for tr_el in tr_root:
                    target = tr_el.get("Target", "")
                    if not target:
                        continue
                    # Resolve relative path from base_dir
                    resolved = os.path.normpath(os.path.join(base_dir, target)).replace(
                        os.sep, "/"
                    )
                    if resolved in orig_entries and resolved not in needed:
                        needed.add(resolved)
                        # Search for sub-rels (drawing.rels, vml.rels)
                        res_dir = os.path.dirname(resolved)
                        res_base = os.path.basename(resolved)
                        sub_rels = f"{res_dir}/_rels/{res_base}.rels"
                        if sub_rels in orig_entries:
                            needed.add(sub_rels)
                            _trace_rels(sub_rels, res_dir)
            except Exception as e:
                logger.warning("Trace rels failed for %s: %s", rels_path, e)

        # Always need base files
        needed.add("[Content_Types].xml")
        needed.add("_rels/.rels")
        needed.add("xl/workbook.xml")
        needed.add("xl/_rels/workbook.xml.rels")

        # The sheet itself
        needed.add(orig_sheet_path)

        # .rels file of the sheet and its recursive dependencies
        sheet_dir = os.path.dirname(orig_sheet_path)
        sheet_base = os.path.basename(orig_sheet_path)
        sheet_rels_path = f"{sheet_dir}/_rels/{sheet_base}.rels"
        if sheet_rels_path in orig_entries:
            needed.add(sheet_rels_path)
            _trace_rels(sheet_rels_path, sheet_dir)

        # Add dependencies from workbook.xml.rels.
        # Keep shared items, customXml, datastore, VBA, etc.,
        # but exclude other sheets (worksheet/chartsheet/dialogsheet)
        # and calcChain (computation chain, invalid after sheet removal).
        for rel_el in rels_root:
            rel_id = rel_el.get("Id", "")
            rel_type = rel_el.get("Type", "").lower()
            rel_target = rel_el.get("Target", "")
            if rel_id == target_r_id:
                continue  # Skip the sheet itself (already added)
            # Exclude relationships to other sheets and invalid calcChain
            if any(
                t in rel_type
                for t in [
                    "worksheet",
                    "chartsheet",
                    "dialogsheet",
                    "calcchain",
                ]
            ):
                continue
            # Resolve path
            if rel_target.startswith("/"):
                resolved = rel_target.lstrip("/")
            else:
                resolved = os.path.normpath(os.path.join("xl", rel_target)).replace(
                    os.sep, "/"
                )
            if resolved in orig_entries:
                needed.add(resolved)
                # Trace sub-relationships (e.g. customXml/_rels/item1.xml.rels)
                res_dir = os.path.dirname(resolved)
                res_base = os.path.basename(resolved)
                sub_rels = f"{res_dir}/_rels/{res_base}.rels"
                if sub_rels in orig_entries:
                    needed.add(sub_rels)
                    _trace_rels(sub_rels, res_dir)

        # Add docProps (core, app, custom) — do not affect sheet loading
        doc_props = [n for n in orig_entries if n.startswith("docProps/")]
        needed.update(doc_props)

        # Add customXml (if present)
        custom_xml = [n for n in orig_entries if n.startswith("customXml/")]
        needed.update(custom_xml)

        # ── PHASE 4: Collect names of removed sheets ──
        other_sheet_names: set[str] = set()
        for sheet_el in sheets_elem.findall(f"{{{NS_MAIN}}}sheet"):
            sn = sheet_el.get("name", "")
            if sn != keep_sheet_name:
                other_sheet_names.add(sn)

        # ── PHASE 5: Modify workbook.xml via string operations ──
        # IMPORTANT: we use string operations, NOT XML parsing,
        # to preserve the original namespace declarations, XML declaration,
        # line endings and all other details of the source file AS-IS.
        new_wb_text = _modify_workbook_xml_text(
            orig_entries["xl/workbook.xml"].decode("utf-8"),
            keep_sheet_name,
            target_r_id,
            other_sheet_names,
        )

        # ── PHASE 6: Modify workbook.xml.rels — remove extra Relationships ──
        new_rels_text = _modify_workbook_rels_text(
            orig_entries["xl/_rels/workbook.xml.rels"].decode("utf-8"),
            target_r_id,
        )

        # ── PHASE 7: Build the output dictionary ──
        output_entries: dict[str, bytes] = {}

        for name in needed:
            if name == "xl/workbook.xml":
                output_entries[name] = new_wb_text.encode("utf-8")
            elif name == "xl/_rels/workbook.xml.rels":
                output_entries[name] = new_rels_text.encode("utf-8")
            elif name.startswith("xl/printerSettings/"):
                # Skip printerSettings — binary files that reference removed sheets
                # cause "Removed Part" errors in Excel
                continue
            else:
                output_entries[name] = orig_entries[name]

        # ── PHASE 7b: Clean .rels files of references to removed printerSettings ──
        for rels_name in list(output_entries.keys()):
            if rels_name.endswith(".rels") and "printerSettings" not in rels_name:
                try:
                    rels_text = output_entries[rels_name].decode("utf-8")
                    if "printerSettings" in rels_text:
                        # Remove Relationship entries pointing to printerSettings
                        cleaned = re.sub(
                            r'<Relationship[^>]*Target="[^"]*printerSettings[^"]*"[^>]*/>\s*',
                            "",
                            rels_text,
                        )
                        output_entries[rels_name] = cleaned.encode("utf-8")
                except Exception:
                    pass

        # ── PHASE 8: Filter Content_Types.xml — remove Override for missing files ──
        if "[Content_Types].xml" in needed:
            ct_text = orig_entries["[Content_Types].xml"].decode("utf-8")
            new_ct_text = _filter_content_types_text(
                ct_text, set(output_entries.keys())
            )
            output_entries["[Content_Types].xml"] = new_ct_text.encode("utf-8")

        # ── PHASE 8: Write new ZIP preserving original compression ──
        # IMPORTANT: MS Excel requires images (PNG, EMF, JPEG) to be
        # STORED (uncompressed), and XML/DATA files — DEFLATED.
        # Use original compression_type if known.
        if os.path.exists(output_path):
            os.remove(output_path)

        def _get_compress_type(name: str) -> int:
            """Determine compression: STORED for images, DEFLATED for everything else.

            MS Office stores images as-is (STORED) because they
            are already compressed. XML and other text data — DEFLATED.
            """
            name_lower = name.lower()
            # Images — no compression (already compressed, DEFLATE does not help)
            if any(
                name_lower.endswith(ext)
                for ext in [
                    ".png",
                    ".emf",
                    ".wmf",
                    ".jpeg",
                    ".jpg",
                    ".gif",
                    ".tiff",
                    ".tif",
                    ".bmp",
                    ".svg",
                ]
            ):
                return zipfile.ZIP_STORED
            return zipfile.ZIP_DEFLATED

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in sorted(output_entries.keys()):
                compress_type = _get_compress_type(name)
                zout.writestr(name, output_entries[name], compress_type=compress_type)


_CT_CACHE: dict[str, str | None] = {}
_CT_CACHE_MAX_SIZE = 1024


def _modify_workbook_xml_text(
    xml_text: str,
    keep_sheet_name: str,
    target_r_id: str,
    other_sheet_names: set[str],
) -> str:
    """Modify workbook.xml via string operations.

    1. Remove extra <sheet> from <sheets>.
    2. Remove <definedName> referencing removed sheets.

    EVERYTHING else is preserved AS-IS (XML declaration, namespace, line endings).
    """

    # ── 1. Replace <sheets> — keep only 1 sheet ──
    def _replace_sheets(m: re.Match) -> str:
        """Callback to replace <sheets> content."""
        open_tag = m.group(1)
        close_tag = m.group(3)
        content = m.group(2)
        # Search for the target sheet by name or r:id
        kept = None
        for sh in re.finditer(r"<sheet[^>]*/>", content):
            sh_tag = sh.group(0)
            name_m = re.search(r'name="([^"]+)"', sh_tag)
            if name_m and name_m.group(1) == keep_sheet_name:
                kept = sh_tag
                break
        # Fallback: by r:id
        if kept is None:
            for sh in re.finditer(r"<sheet[^>]*/>", content):
                sh_tag = sh.group(0)
                rid_m = re.search(r'r:id="([^"]+)"', sh_tag)
                if rid_m and rid_m.group(1) == target_r_id:
                    kept = sh_tag
                    break
        if kept:
            return f"{open_tag}\n{kept}\n{close_tag}"
        return m.group(0)  # fallback: unchanged

    xml_text = re.sub(
        r"(<sheets[^>]*>)(.*?)(</sheets>)",
        _replace_sheets,
        xml_text,
        count=1,
        flags=re.DOTALL,
    )

    # ── 2. Clean definedNames ──
    def _filter_defined_names(m: re.Match) -> str:
        """Callback: remove definedName referencing other_sheet_names."""
        dn_block = m.group(0)
        # Find tag boundaries
        dn_open_m = re.match(r"(<definedNames[^>]*>)", dn_block)
        if not dn_open_m:
            return dn_block
        dn_open = dn_open_m.group(1)
        # Find closing tag
        close_idx = dn_block.rfind("</definedNames>")
        if close_idx == -1:
            return dn_block
        content = dn_block[len(dn_open) : close_idx]

        kept_lines = []
        for dn_match_inner in re.finditer(
            r"<definedName[^>]*>.*?</definedName>", content, re.DOTALL
        ):
            dn_xml = dn_match_inner.group(0)
            dn_text = re.sub(r"<[^>]+>", "", dn_xml).strip()  # extract text content
            formula = dn_text
            should_remove = False
            for deleted_name in other_sheet_names:
                if f"'{deleted_name}'!" in formula or formula.startswith(
                    f"{deleted_name}!"
                ):
                    should_remove = True
                    break
            if not should_remove:
                # Update localSheetId to 0 (kept sheet is now the only one)
                dn_xml = re.sub(r'localSheetId="[^"]+"', 'localSheetId="0"', dn_xml)
                kept_lines.append(dn_xml)

        if not kept_lines:
            # definedNames empty — remove the entire block
            return ""
        return dn_open + "".join(kept_lines) + "</definedNames>"

    xml_text = re.sub(
        r"<definedNames[^>]*>.*?</definedNames>",
        _filter_defined_names,
        xml_text,
        count=1,
        flags=re.DOTALL,
    )

    # ── 3. Clean bookViews/workbookView — reset activeTab/firstSheet ──
    def _fix_workbook_view(m: re.Match) -> str:
        tag = m.group(0)
        tag = re.sub(r'activeTab="[^"]*"', 'activeTab="0"', tag)
        tag = re.sub(r'firstSheet="[^"]*"', 'firstSheet="0"', tag)
        return tag

    xml_text = re.sub(
        r"<(?:[\w\-]+:)?workbookView\b[^>]*/>", _fix_workbook_view, xml_text
    )

    # ── 4. Remove customWorkbookViews ──
    xml_text = re.sub(
        r"<customWorkbookViews[^>]*>.*?</customWorkbookViews>",
        "",
        xml_text,
        flags=re.DOTALL,
    )

    return xml_text


def _modify_workbook_rels_text(
    rels_text: str,
    target_r_id: str,
) -> str:
    """Modify workbook.xml.rels — remove Relationships for other sheets.

    Leaves ONLY:
      - worksheet (kept sheet)
      - styles
      - theme
      - sharedStrings

    EVERYTHING else (XML declaration, formatting) is preserved AS-IS.
    Uses re.sub to remove individual <Relationship .../> lines.
    """

    def _keep_relevant_rels(m: re.Match) -> str:
        """Callback: return Relationship line only if it is needed."""
        rel = m.group(0)
        rid_m = re.search(r'Id="([^"]+)"', rel)
        rtype_m = re.search(r'Type="([^"]+)"', rel)
        rid = rid_m.group(1) if rid_m else ""
        rtype = rtype_m.group(1).lower() if rtype_m else ""

        # Always keep the target sheet
        if rid == target_r_id:
            return rel
        # Remove other sheet relationships and invalid calcChain
        if any(
            t in rtype
            for t in [
                "worksheet",
                "chartsheet",
                "dialogsheet",
                "calcchain",
            ]
        ):
            return ""
        # Keep remaining relationships (styles, theme, sharedStrings, customXml, VBA, etc.)
        return rel

    return re.sub(r"<Relationship[^>]*/>", _keep_relevant_rels, rels_text)


def _filter_content_types_text(
    ct_text: str,
    existing_files: set[str],
) -> str:
    """Filter [Content_Types].xml — remove Override for non-existent files.

    Args:
        ct_text: Original text [Content_Types].xml.
        existing_files: Set of file paths in output ZIP.

    Returns:
        Filtered XML text (EVERYTHING else AS-IS).
    """

    def _filter_override(m: re.Match) -> str:
        """Callback: return Override only if file exists."""
        override_line = m.group(0)
        pn_m = re.search(r'PartName="([^"]+)"', override_line)
        if pn_m:
            part_name = pn_m.group(1)
            if part_name.startswith("/"):
                clean_name = part_name[1:]
            else:
                clean_name = part_name
            if clean_name not in existing_files:
                return ""  # Remove
        return override_line

    return re.sub(r"<Override[^>]*>(?:</Override>)?", _filter_override, ct_text)


def _infer_content_type(path: str) -> str | None:
    """Determine OOXML ContentType by file path."""
    if path in _CT_CACHE:
        return _CT_CACHE[path]

    result: str | None = None
    path_lower = path.lower()

    if path_lower.endswith(".xml"):
        if "drawing" in path_lower and "rels" not in path_lower:
            result = "application/vnd.openxmlformats-officedocument.drawing+xml"
        elif "vml" in path_lower:
            result = "application/vnd.openxmlformats-officedocument.vmlDrawing"
    elif path_lower.endswith(".bin"):
        result = "application/vnd.openxmlformats-officedocument.oleObject"
    elif path_lower.endswith(".rels"):
        result = "application/vnd.openxmlformats-package.relationships+xml"
    elif path_lower.endswith(".png"):
        result = "image/png"
    elif path_lower.endswith(".jpeg") or path_lower.endswith(".jpg"):
        result = "image/jpeg"
    elif path_lower.endswith(".emf"):
        result = "image/x-emf"
    elif path_lower.endswith(".wmf"):
        result = "image/x-wmf"
    elif path_lower.endswith(".gif"):
        result = "image/gif"
    elif path_lower.endswith(".tiff") or path_lower.endswith(".tif"):
        result = "image/tiff"
    elif path_lower.endswith(".bmp"):
        result = "image/bmp"
    elif path_lower.endswith(".svg"):
        result = "image/svg+xml"

    if len(_CT_CACHE) >= _CT_CACHE_MAX_SIZE:
        # Evict oldest entries (dict preserves insertion order)
        while len(_CT_CACHE) >= _CT_CACHE_MAX_SIZE:
            _CT_CACHE.popitem(last=False)
    _CT_CACHE[path] = result
    return result


def _validate_split_file(path: str) -> bool:
    """Verify a split .xlsx file has valid sheet XML and can be opened.

    Uses ValidationPipeline for comprehensive verification
    (structural + schema + split-quality levels).

    Returns True if the file is valid, False if it should be deleted.
    """
    from app.services.validator import validate_split_file

    result = validate_split_file(
        path,
        has_images_in_original=True,  # Conservative: assume images were present
    )
    if not result.is_valid:
        for issue in result.errors:
            logger.warning(
                "Invalid split file %s: [%s] %s",
                os.path.basename(path),
                issue.level,
                issue.message,
            )
    return result.is_valid


def _safe_filename(name: str) -> str:
    """Clean filename, preserving Unicode characters.

    Removes:
      - Characters forbidden in OS filenames: < > : " / \\ | ? *
      - Control characters (0x00-0x1f)
      - Decorative Unicode: ☆ ★ ● ○ ◆ ◇ ■ □ etc.
      - Surrogate pairs (invalid Unicode)
      - Multiple underscores/dots/spaces → single

    Preserves:
      - Chinese characters (CJK)
      - Cyrillic
      - Latin and digits

    Args:
        name: Original filename.

    Returns:
        Safe filename with preserved Cyrillic/CJK.
    """
    # Remove surrogate pairs (invalid Unicode from broken encodings)
    result = name.encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")
    result = _ILLEGAL_FS_CHARS_RE.sub("_", result)
    result = _DECORATIVE_CHARS_RE.sub("", result)
    result = _MULTI_SEP_RE.sub("_", result)
    return result.strip("_ .")


def _collect_related_files(
    zip_entries: dict[str, bytes],
    removed_sheet: str,
    files_to_remove: set[str],
) -> None:
    """Collect all files related to the removed sheet (.rels, drawings, VML, charts).

    Args:
        zip_entries: Dictionary {zip_name: content}.
        removed_sheet: Path to the removed sheet (e.g. 'xl/worksheets/sheet2.xml').
        files_to_remove: Set for adding found files.
    """
    # .rels file for the sheet
    base = os.path.basename(removed_sheet)
    removed_rels = f"xl/worksheets/_rels/{base}.rels"
    if removed_rels in zip_entries:
        files_to_remove.add(removed_rels)

        # Find related drawings, VML, charts
        try:
            sr_root = ET.fromstring(zip_entries[removed_rels])
            # OOXML relationship targets resolve relative to the package part
            # (e.g. xl/worksheets/), NOT relative to the .rels directory
            # (e.g. xl/worksheets/_rels/). Go up one extra level.
            sr_dir = os.path.dirname(os.path.dirname(removed_rels))
            for sr_el in sr_root:
                sr_target = sr_el.get("Target", "")
                # Resolve relative path (../drawings/drawing1.xml)
                resolved = os.path.normpath(os.path.join(sr_dir, sr_target))
                resolved = resolved.replace(os.sep, "/")
                files_to_remove.add(resolved)

                # Recursive: .rels for drawing, VML
                resolved_base = os.path.basename(resolved)
                resolved_dir = os.path.dirname(resolved)
                resolved_rels = f"{resolved_dir}/_rels/{resolved_base}.rels"
                if resolved_rels in zip_entries:
                    files_to_remove.add(resolved_rels)
        except Exception as e:
            logger.debug("Relationship resolution failed: %s", e)


def _clean_named_ranges(
    wb_root: ET.Element,
    deleted_sheet_names: set[str],
    keep_sheet_name: str,
) -> None:
    """Clean definedNames (named ranges) referencing removed sheets.

    This is a KEY step to eliminate the Excel error:
    "Removed Feature: Named range from /xl/workbook.xml part (Workbook)"

    Algorithm:
      1. Find the <definedNames> element in workbook.xml.
      2. For each <definedName>, check if it references a removed sheet.
      3. Sheet reference in definedName usually looks like: SheetName!$A$1
         or is wrapped in single quotes if the name has spaces: 'Sheet Name'!$A$1.
      4. Remove all definedNames referencing removed sheets.

    Args:
        wb_root: Root element of workbook.xml.
        deleted_sheet_names: Set of removed sheet names.
        keep_sheet_name: Name of the kept sheet.
    """
    defined_names_elem = wb_root.find(f"{{{NS_MAIN}}}definedNames")
    if defined_names_elem is None:
        return  # No named ranges — nothing to clean

    names_to_remove: list[ET.Element] = []

    for dn in defined_names_elem.findall(f"{{{NS_MAIN}}}definedName"):
        # DefinedName text is a formula with a sheet reference
        formula = (dn.text or "").strip()
        name_attr = dn.get("name", "")

        # Check if definedName references a removed sheet
        # Reference patterns:
        #   'Sheet Name'!$A$1:$B$2
        #   SheetName!$A$1
        #   SheetName!$A$1:$B$2
        should_remove = False

        for deleted_name in deleted_sheet_names:
            # Check with quotes (for names with spaces/special chars)
            if f"'{deleted_name}'!" in formula:
                should_remove = True
                break
            # Check without quotes
            if formula.startswith(f"{deleted_name}!"):
                should_remove = True
                break
            # Check for inclusion (less precise but covers edge cases)
            # Search for pattern: word boundary + sheet name + !
            if re.search(rf"\b{re.escape(deleted_name)}!", formula):
                should_remove = True
                break

        # Also check local names (localSheetId attribute)
        if not should_remove:
            local_sheet_id = dn.get("localSheetId")
            if local_sheet_id is not None and local_sheet_id != "0":
                # After removing all other sheets, the remaining sheet
                # becomes the only one with index 0.
                # Update localSheetId to 0.
                dn.set("localSheetId", "0")

        if should_remove:
            names_to_remove.append(dn)
            logger.debug(
                "Removed definedName '%s' (reference to removed sheet)",
                name_attr,
            )

    for dn in names_to_remove:
        defined_names_elem.remove(dn)

    # If definedNames is empty after cleanup — remove the element entirely
    if len(defined_names_elem) == 0:
        wb_root.remove(defined_names_elem)


def preallocate_split_paths(
    tasks: list[tuple[str, str, list[str], str]],
    output_dir: str,
) -> dict[tuple[str, str], str]:
    """Deterministic pre-allocation of paths for all sheets.

    EXECUTED IN THE MAIN THREAD (single thread, deterministic).

    Algorithm:
      1. Collect all tasks (source + sheet + label) into a flat list.
      2. Sort by (source_path, sheet_name) — deterministic order.
      3. Compute the target path for each task.
      4. On name collision — resolve sequentially (_1, _2, ...).
         Since the list is sorted, resolution is 100% deterministic.
      5. Return dict {(source_path, sheet_name) -> abs_output_path}.

    Args:
        tasks: List of tuples (source_path, output_dir, sheet_names, file_label)
               — same format as in split_many_parallel.
        output_dir: Output directory.

    Returns:
        Dictionary mapping (source_path, sheet_name) to a unique
        absolute output file path.
    """
    path_registry: set[str] = set()
    path_map: dict[tuple[str, str], str] = {}

    # 1. Collect flat list (source_path, sheet_name, file_label)
    sheet_tasks: list[tuple[str, str, str]] = []
    for source_path, _out_dir, sheet_names, file_label in tasks:
        for sheet_name in sheet_names:
            sheet_tasks.append((source_path, sheet_name, file_label or ""))

    # 2. Deterministic sort
    sheet_tasks.sort(key=lambda t: (t[0], t[1], t[2]))

    # 3-4. Pre-compute paths with deterministic collision resolution
    for source_path, sheet_name, file_label in sheet_tasks:
        safe_label = _safe_filename(file_label)[:50] if file_label else ""
        safe_sheet = _safe_filename(sheet_name)[:50]
        if safe_label:
            output_filename = f"{safe_label}_{safe_sheet}.xlsx"
        else:
            output_filename = f"{safe_sheet}.xlsx"

        output_path = os.path.join(output_dir, output_filename)
        base_no_ext = os.path.splitext(output_filename)[0]
        ext = ".xlsx"

        # Deterministic collision resolution (check by set, not filesystem)
        counter = 1
        while output_path in path_registry:
            output_path = os.path.join(output_dir, f"{base_no_ext}_{counter}{ext}")
            counter += 1

        path_registry.add(output_path)
        path_map[(source_path, sheet_name)] = output_path

    return path_map


def _verify_xlsx_integrity(file_path: str) -> tuple[bool, str]:
    """Verify a split .xlsx file (LENIENT check, like Microsoft Excel).

    File is considered CORRUPTED (returns False) only if openpyxl cannot
    load it even in read_only mode — i.e. on fatal exceptions:
      - zipfile.BadZipFile (archive is not a ZIP)
      - InvalidFileException (corrupted OOXML structure)

    Args:
        file_path: Path to .xlsx file.

    Returns:
        Tuple (is_valid: bool, error_message: str).
        error_message empty if file is valid.
    """
    import warnings

    import openpyxl

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            _ = wb.sheetnames
            wb.close()
    except zipfile.BadZipFile as e:
        return False, f"BadZipFile: {e}"
    except Exception as e:
        exc_name = type(e).__name__
        if exc_name in (
            "InvalidFileException",
            "InvalidFormatException",
            "LoadWorkbookException",
        ):
            return False, f"{exc_name}: {e}"
        # All other exceptions are non-fatal (warnings, DrawingML, etc.)
        return True, ""

    return True, ""


def _copy_source_as_fallback(
    source_path: str,
    output_path: str,
) -> None:
    """Copy source file as-is — last chance before corrupted.

    Used when both ZIP and openpyxl failed to extract the sheet.
    Verifies the source file opens via openpyxl (lenient check).
    If the file is valid — copies it to the output path.

    This prevents files from ending up in corrupted_cards that are
    functionally valid but cannot be split
    due to non-standard OOXML structure (WPS Office, etc.).

    Args:
        source_path: Path to the source .xlsx file.
        output_path: Path for saving.

    Raises:
        ValueError: If the source file cannot be opened.
    """
    try:
        from app.services.validator import validate_split_file_lenient

        result = validate_split_file_lenient(source_path)
        if not result.is_valid:
            raise ValueError(
                f"Source file cannot be opened: {result.errors[0].message if result.errors else 'unknown'}"
            )
        shutil.copy2(source_path, output_path)
    except ImportError:
        # Fallback: just try to copy if validator not available
        shutil.copy2(source_path, output_path)


def _extract_to_path_worker(
    source_path: str,
    output_path: str,
    sheet_name: str,
) -> dict[str, Any]:
    """Worker function: extract one sheet to a pre-allocated path.

    Runs in a separate process. Does NOT check file existence —
    path uniqueness guaranteed by the main thread via preallocate_split_paths.

    Calls _extract_sheet(), which tries ZIP method, then openpyxl fallback.
    If both methods fail — file is considered corrupted.

    Args:
        source_path: Path to the source .xlsx file.
        output_path: Absolute path for saving (already guaranteed unique).
        sheet_name: Sheet name to extract.

    Returns:
        Dictionary with results:
          - "path": output_path on success, None on error
          - "error": error message or None
          - "used_fallback": True if openpyxl fallback used
          - "source_basename": os.path.basename(source_path)
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    splitter = CardSplitter(max_workers=1)
    result: dict[str, Any] = {
        "path": None,
        "error": None,
        "used_fallback": False,
        "source_basename": os.path.basename(source_path),
        "source_path": source_path,
        "sheet_name": sheet_name,
    }
    try:
        splitter._extract_sheet(source_path, output_path, sheet_name)
        if splitter.openpyxl_fallback_count > 0:
            result["used_fallback"] = True
        result["path"] = output_path
    except Exception as e:
        err_msg = f"Sheet '{sheet_name}' from {os.path.basename(source_path)}: {e}"
        logger.error("Split error: %s", err_msg)
        result["error"] = err_msg
    return result


def _detect_xinyuan_boundaries(
    source_path: str,
    sheet_name: str,
) -> list[TableBoundary]:
    """Detect operation boundaries by marker '鑫源汽车'.

    Special detector for mega-files (e.g. 4_G01P作业指导书, G01P后备箱, etc.),
    where each operational card starts with '鑫源汽车'.
    Cards are positioned vertically with fixed spacing (36-37 rows).

    Args:
        source_path: Path to .xlsx file.
        sheet_name: Sheet name.

    Returns:
        List of TableBoundary for each found operation.
    """

    boundaries: list[TableBoundary] = []
    reader = ExcelReader(source_path)
    try:
        if sheet_name not in reader.sheet_names:
            return boundaries

        ws = reader.get_sheet(sheet_name)
        max_row = ws.max_row or 0
        if max_row < 10:
            return boundaries

        max_col = min((ws.max_column or 10) + 1, 20)

        # Search for all rows with '鑫源汽车' in the first 10 columns
        marker_rows: list[int] = []
        for r in range(1, max_row + 1):
            for c in range(1, min(max_col, 10)):
                val = ws.cell_value(r, c)
                if val is not None and "鑫源汽车" in str(val):
                    marker_rows.append(r)
                    break

        if len(marker_rows) < 2:
            return boundaries

        # Compute step (median of intervals)
        spacings = [
            marker_rows[i + 1] - marker_rows[i] for i in range(len(marker_rows) - 1)
        ]
        step = sorted(spacings)[len(spacings) // 2]  # median

        # Check step stability (>60% of intervals within ±5 of median)
        consistent = sum(1 for s in spacings if abs(s - step) <= 5)
        if consistent < len(spacings) * 0.6:
            logger.debug(
                "Xinyuan boundaries: inconsistent spacing (step=%d, consistent=%d/%d)",
                step,
                consistent,
                len(spacings),
            )
            return boundaries

        # Build boundaries: each '鑫源汽车' — start of a new card
        for idx, marker_row in enumerate(marker_rows):
            # Data boundary: from current marker to next (or end of file)
            if idx + 1 < len(marker_rows):
                data_end = marker_rows[idx + 1] - 1
            else:
                data_end = max_row

            # Extract operation name: search for CJK text in the marker row
            # (columns B-J, skipping column A where the marker is)
            op_name = ""
            for c in range(2, min(max_col, 10)):
                val = ws.cell_value(marker_row, c)
                if val is not None:
                    val_str = str(val).strip()
                    if len(val_str) > 2 and re.search(r"[\u4e00-\u9fff]", val_str):
                        op_name = val_str
                        break

            boundaries.append(
                TableBoundary(
                    header_row=marker_row,
                    data_start=marker_row + 1,
                    data_end=data_end,
                    operation_name=op_name,
                    source_path=source_path,
                    sheet_name=sheet_name,
                    card_label=(
                        f"{idx + 1:03d}_{_safe_filename(op_name)[:30]}"
                        if op_name
                        else f"Op{idx + 1:03d}"
                    ),
                )
            )

        logger.info(
            "Xinyuan boundaries: found %d cards (step=%d rows) in %s",
            len(boundaries),
            step,
            os.path.basename(source_path),
        )

    finally:
        reader.close()

    return boundaries


def find_table_boundaries(
    source_path: str,
    sheet_name: str,
    min_confidence: float = 0.35,
) -> list[TableBoundary]:
    """Detect table (operation) boundaries within a single sheet.

    Universal detector for any brand or file structure.
    Hard limit max_row <= 500 REMOVED — all sheets are analyzed.
    Each found boundary gets a confidence score for filtering
    false positives (fewer false positives on small files).

    Some formats: tables may lack an explicit qty column (qty_col=0).
    For such cases, confidence is computed without qty.

    Args:
        source_path: Path to .xlsx file.
        sheet_name: Sheet name to analyze.
        min_confidence: Minimum confidence threshold (0.0-1.0).
                        Lowered from 0.3 to 0.2 to support wider formats.

    Returns:
        List of TableBoundary with boundaries of each table.
    """
    from app.services.heuristic_analyzer import HeuristicAnalyzer

    boundaries: list[TableBoundary] = []

    reader = ExcelReader(source_path)
    try:
        if sheet_name not in reader.sheet_names:
            return boundaries

        ws = reader.get_sheet(sheet_name)
        start_search = 1
        max_row = ws.max_row or 0

        if max_row < 3:
            return boundaries

        # ── High-priority: '鑫源汽车' marker detection ──
        # Check FIRST, before HeuristicAnalyzer, because
        # find_part_table finds boundaries NOT matching 鑫源汽车
        # (offset by ~20 rows), leading to 2 cards in one file.
        xinyuan_first = _detect_xinyuan_boundaries(source_path, sheet_name)
        if xinyuan_first:
            logger.info(
                "Xinyuan (primary): found %d cards in %s",
                len(xinyuan_first),
                os.path.basename(source_path),
            )
            reader.close()
            return xinyuan_first

        max_tables = 500

        for table_idx in range(max_tables):
            if start_search >= max_row:
                break

            table_info = HeuristicAnalyzer.find_part_table(ws, start_row=start_search)
            if table_info is None:
                break

            header_row, part_no_col, qty_col, name_col = table_info

            if header_row < start_search:
                break

            # Determine operation_name
            operation_name = HeuristicAnalyzer.extract_operation_name(ws, header_row)

            # Determine last data row (data_end)
            data_end = _find_table_data_end(ws, header_row, max_row, part_no_col)

            # Confidence scoring: filter out false positives
            # IMPORTANT: qty_col may be 0 in some formats — confidence
            # is computed without qty (with lower threshold)
            confidence = _compute_boundary_confidence(
                ws,
                header_row,
                data_end,
                part_no_col,
                qty_col if qty_col and qty_col > 0 else 0,
                name_col if name_col and name_col > 0 else 0,
            )
            if confidence < min_confidence:
                logger.debug(
                    "Boundary at row %d rejected: confidence %.2f < %.2f",
                    header_row,
                    confidence,
                    min_confidence,
                )
                start_search = header_row + 1
                continue

            boundaries.append(
                TableBoundary(
                    header_row=header_row,
                    data_start=header_row + 1,
                    data_end=data_end,
                    operation_name=operation_name,
                    source_path=source_path,
                    sheet_name=sheet_name,
                    card_label=f"{table_idx + 1:03d}_{_safe_filename(operation_name)[:30]}"
                    if operation_name
                    else f"Op{table_idx + 1:03d}",
                )
            )

            start_search = data_end + 1

    finally:
        reader.close()

    # High-priority fallback: marker '鑫源汽车' (mega-files with 83+ cards)
    if not boundaries and max_row > 50:
        boundaries = _detect_xinyuan_boundaries(source_path, sheet_name)

    # Fallback: inspection table detection (检验项目 pattern)
    if not boundaries:
        boundaries = _detect_inspection_boundaries(source_path, sheet_name)

    # Fallback: universal repeating pattern row detector
    # (for formats where find_part_table may miss tables)
    if not boundaries and max_row > 100:
        boundaries = _detect_repeating_pattern_boundaries(source_path, sheet_name)

    # Mega-sheet force: if sheet is very large (500+ rows), and only
    # few boundaries found (less than 5% rows covered) — heuristics may have
    # missed most tables. Force running
    # universal repeating pattern detector.
    if boundaries and max_row > 200:
        covered_rows = sum(b.data_end - b.header_row for b in boundaries)
        coverage_ratio = covered_rows / max(max_row, 1)
        if coverage_ratio < 0.3 or len(boundaries) < 3:
            logger.info(
                "Mega-sheet (%d rows, %d boundaries, %.1f%% coverage) "
                "— forcing re-detection",
                max_row,
                len(boundaries),
                coverage_ratio * 100,
            )
            # Prefer marker-based detection for multi-sheet files
            xinyuan_boundaries = _detect_xinyuan_boundaries(
                source_path,
                sheet_name,
            )
            if xinyuan_boundaries and len(xinyuan_boundaries) > len(boundaries):
                boundaries = xinyuan_boundaries
                logger.info(
                    "Xinyuan detection found %d boundaries",
                    len(boundaries),
                )
            else:
                pattern_boundaries = _detect_repeating_pattern_boundaries(
                    source_path,
                    sheet_name,
                )
                if pattern_boundaries and len(pattern_boundaries) > len(boundaries):
                    boundaries = pattern_boundaries
                    logger.info(
                        "Repeating pattern detection found %d boundaries",
                        len(boundaries),
                    )

    # ── Boundary snapping: close gaps between operations ──
    # Ensures every row between consecutive operations is assigned.
    # Without this, rows between data_end and next header_row are lost.
    if boundaries:
        boundaries.sort(key=lambda b: b.header_row)
        for i in range(len(boundaries) - 1):
            if boundaries[i].data_end < boundaries[i + 1].header_row - 1:
                boundaries[i].data_end = boundaries[i + 1].header_row - 1

    # ── Validation: reject boundary sets with inconsistent spacing ──
    # If intervals between boundaries vary wildly (>50% from median),
    # it's likely a false positive (not real operations but random data patterns).
    if len(boundaries) >= 3:
        spacings = [
            boundaries[i + 1].header_row - boundaries[i].header_row
            for i in range(len(boundaries) - 1)
        ]
        if spacings:
            median_spacing = sorted(spacings)[len(spacings) // 2]
            if median_spacing > 0:
                consistent = sum(
                    1
                    for s in spacings
                    if abs(s - median_spacing) <= median_spacing * 0.5
                )
                if consistent < len(spacings) * 0.5:
                    logger.warning(
                        "Boundary set rejected: inconsistent spacing "
                        "(median=%d, consistent=%d/%d)",
                        median_spacing,
                        consistent,
                        len(spacings),
                    )
                    boundaries = []

    return boundaries


def _compute_boundary_confidence(
    ws: Any,
    header_row: int,
    data_end: int,
    part_no_col: int,
    qty_col: int,
    name_col: int,
) -> float:
    """Compute confidence in table boundaries (0.0-1.0).

    Score based on:
      - Keyword count in header (up to 0.4)
      - Data density: non-empty rows / total rows (up to 0.3)
      - Valid part number count in data (up to 0.3)

    IMPORTANT: qty_col may be 0 (some formats) — in this case
    qty check is skipped, confidence is lowered via a
    lower min_confidence threshold.
    """
    from app.services.heuristic_analyzer import (
        NAME_KEYWORDS,
        PART_NO_KEYWORDS,
        QTY_KEYWORDS,
        HeuristicAnalyzer,
    )
    from app.services.normalizer import is_valid_part_number

    score = 0.0

    # 1. Header quality (0.0-0.4)
    max_check_col = min((ws.max_column or 10) + 1, 50)
    header_non_empty = 0
    header_keywords = 0
    for hc in range(1, max_check_col):
        hv = HeuristicAnalyzer.get_cell_value(ws, header_row, hc)
        if hv is not None and str(hv).strip():
            header_non_empty += 1
            hv_lower = str(hv).strip().lower()
            if any(kw in hv_lower for kw in PART_NO_KEYWORDS):
                header_keywords += 1
            if qty_col > 0 and any(kw in hv_lower for kw in QTY_KEYWORDS):
                header_keywords += 1
            if name_col > 0 and any(kw in hv_lower for kw in NAME_KEYWORDS):
                header_keywords += 1

    if header_non_empty >= 3:
        score += 0.2
    elif header_non_empty >= 2:
        score += 0.1
    score += min(header_keywords * 0.1, 0.2)

    # 2. Data density (0.0-0.3)
    data_rows = data_end - header_row
    if data_rows > 0:
        non_empty_data = 0
        sample_start = header_row + 1
        sample_end = min(data_end + 1, header_row + 50)
        sample_count = sample_end - sample_start
        for r in range(sample_start, sample_end):
            v = HeuristicAnalyzer.get_cell_value(ws, r, part_no_col)
            if v is not None and str(v).strip():
                non_empty_data += 1
        if sample_count > 0:
            density = non_empty_data / sample_count
            score += density * 0.3

    # 3. Valid part numbers (0.0-0.3)
    valid_pn = 0
    total_pn = 0
    check_end = min(data_end + 1, header_row + 30)
    for r in range(header_row + 1, check_end):
        v = HeuristicAnalyzer.get_cell_value(ws, r, part_no_col)
        if v is not None:
            total_pn += 1
            pn_str = str(v).strip()
            if is_valid_part_number(pn_str):
                valid_pn += 1
    if total_pn > 0:
        pn_ratio = valid_pn / total_pn
        score += pn_ratio * 0.3

    return min(score, 1.0)


# Keywords for detecting inspection tables
_INSPECTION_HEADER_KW = "检验项目"
_INSPECTION_SUBHEADER_KW = "作业内容图示"


def _detect_inspection_boundaries(
    source_path: str,
    sheet_name: str,
) -> list[TableBoundary]:
    """Detect inspection table boundaries (检验作业指导书).

    Searches for repeating blocks with header "检验项目" in column B.
    Each block contains a quality inspection operation.

    Args:
        source_path: Path to .xlsx file.
        sheet_name: Sheet name.

    Returns:
        List of TableBoundary for each inspection operation.
    """

    boundaries: list[TableBoundary] = []
    reader = ExcelReader(source_path)
    try:
        if sheet_name not in reader.sheet_names:
            return boundaries

        ws = reader.get_sheet(sheet_name)
        max_row = ws.max_row or 0
        if max_row < 3:
            return boundaries

        # Find all rows with "检验项目" in column B (col 2)
        header_rows: list[int] = []
        for r in range(1, max_row + 1):
            val = ws.cell_value(r, 2)
            if val is not None and _INSPECTION_HEADER_KW in str(val):
                header_rows.append(r)

        if len(header_rows) < 2:
            return boundaries

        # Determine step between headers (median of intervals)
        spacings = [
            header_rows[i + 1] - header_rows[i] for i in range(len(header_rows) - 1)
        ]
        if not spacings:
            return boundaries
        step = sorted(spacings)[len(spacings) // 2]  # median

        # Check that step is stable (>50% of intervals within ±3 of median)
        consistent = sum(1 for s in spacings if abs(s - step) <= 3)
        if consistent < len(spacings) * 0.5:
            return boundaries

        # Group headers: each header — a separate operation,
        # data goes until the next header

        # Pre-compute title rows for all header rows
        title_rows: list[int] = []
        for header_row in header_rows:
            title_row = header_row
            for tr in range(max(1, header_row - 5), header_row):
                tr_val = ws.cell_value(tr, 4)  # column D
                if tr_val is not None:
                    tr_str = str(tr_val).strip()
                    if "检验" in tr_str or "作业指导" in tr_str or "指导书" in tr_str:
                        title_row = tr
                        break
            title_rows.append(title_row)

        for group_idx, header_row in enumerate(header_rows):
            cur_title = title_rows[group_idx]
            # data_end: until next title_row (not header_row!), to avoid overlap
            if group_idx + 1 < len(header_rows):
                next_title = title_rows[group_idx + 1]
                data_end = next_title - 1
            else:
                data_end = max_row

            # Extract operation name from column D of header_row
            op_name = ""
            op_val = ws.cell_value(header_row, 4)
            if op_val is not None and str(op_val).strip():
                op_name = str(op_val).strip()

            boundaries.append(
                TableBoundary(
                    header_row=cur_title,
                    data_start=header_row,
                    data_end=data_end,
                    operation_name=op_name,
                    source_path=source_path,
                    sheet_name=sheet_name,
                    card_label=(
                        f"{group_idx + 1:03d}_{_safe_filename(op_name)[:30]}"
                        if op_name
                        else f"Op{group_idx + 1:03d}"
                    ),
                )
            )

    finally:
        reader.close()

    return boundaries


def _find_table_data_end(
    ws: Any,
    header_row: int,
    max_row: int,
    part_no_col: int,
) -> int:
    """Find the last data row of a table.

    Determines the boundary between the current table and the next operation.
    Triggers on:
      1. Header row of the next table (contains PART_NO_KEYWORDS)
      2. Next operation title (row with CJK text where part_no_col is empty)
      3. 5+ fully empty rows in a row (ALL columns empty)
      4. Abrupt row format change (merges, empty columns)

    IMPORTANT: empty-run checks the ENTIRE row for emptiness, not just part_no column.
    In mega-files, part numbers occupy the first few rows of an operation,
    then come instruction and image rows where part_no_col is empty.
    Checking only part_no_col would cut the operation after 2 rows.
    """
    from app.services.heuristic_analyzer import PART_NO_KEYWORDS

    CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

    empty_run = 0
    max_scan = min(max_row - header_row, 500)
    for r in range(header_row + 1, header_row + max_scan + 1):
        if r > max_row:
            break

        # Count non-empty cells across ALL scanned columns to determine
        # if the ENTIRE row is empty (not just part_no column)
        non_empty = 0
        row_values_check: list[str] = []
        max_check_col = min((ws.max_column or 10) + 1, 200)
        for c in range(1, max_check_col):
            v = ws.cell_value(r, c)
            if v is not None:
                non_empty += 1
                rv = str(v).strip().lower()
                if len(rv) < 50:
                    row_values_check.append(rv)

        if non_empty >= 2:
            has_part_no_keyword = any(
                any(kw in rv for kw in PART_NO_KEYWORDS) for rv in row_values_check
            )
            if has_part_no_keyword:
                return r - 1

        # ── Operation title detection ──
        # If part_no_col is empty but the row has CJK text (operation title),
        # this is the next operation boundary
        pn_val = ws.cell_value(r, part_no_col)
        pn_is_empty = pn_val is None or (isinstance(pn_val, str) and not pn_val.strip())

        if pn_is_empty and non_empty >= 1:
            # Check: is there CJK text in the row (operation title indicator)
            has_cjk = False
            for c in range(1, max_check_col):
                v = ws.cell_value(r, c)
                if v is not None and CJK_RE.search(str(v)):
                    has_cjk = True
                    break
            if has_cjk:
                # Check: is this just an empty row with a single value
                # If CJK text and part_no_col empty — likely an operation title
                # Check that the next row is also empty or contains a header
                if r + 1 <= max_row:
                    next_pn = ws.cell_value(r + 1, part_no_col)
                    next_is_empty = next_pn is None or (
                        isinstance(next_pn, str) and not next_pn.strip()
                    )
                    if next_is_empty:
                        # Two consecutive empty part_no with CJK text — boundary
                        return r - 1
                # Lone CJK row with empty part_no — also a boundary
                # (for formats where headers are tightly packed)
                if r - header_row > 3:
                    return r - 1

        # ── Empty-run detection: check FULL row emptiness ──
        # Only count as "empty" when ALL scanned columns are empty.
        # Part-number column being empty is normal for instruction/image rows.
        if non_empty == 0:
            empty_run += 1
            if empty_run >= 5:
                return r - 5
        else:
            empty_run = 0

    return min(max_row, header_row + max_scan)


# ═══════════════════════════════════════════════════════════════════════
# UNIVERSAL REPEATING PATTERN DETECTOR
# ═══════════════════════════════════════════════════════════════════════


def _detect_repeating_pattern_boundaries(
    source_path: str,
    sheet_name: str,
) -> list[TableBoundary]:
    """Detect table boundaries by searching for repeating row patterns.

    Used as a universal fallback for formats where
    find_part_table() may miss tables due to non-standard
    headers or missing explicit qty/name columns.

    Algorithm:
      1. Find rows where column A contains numbers (part-number indicator)
      2. Group sequential data blocks
      3. Split blocks by empty rows or header rows

    Returns:
        List of TableBoundary.
    """
    from app.services.heuristic_analyzer import HeuristicAnalyzer

    boundaries: list[TableBoundary] = []
    reader = ExcelReader(source_path)
    try:
        if sheet_name not in reader.sheet_names:
            return boundaries

        ws = reader.get_sheet(sheet_name)
        max_row = ws.max_row or 0
        if max_row < 20:
            return boundaries

        CJK_RE = re.compile(r"[一-鿿㐀-䶿]")

        # Scan all rows: search for data blocks (part-number in columns A-D)
        # Universal search: part-numbers can be in any of the first 4 columns
        data_blocks: list[tuple[int, int]] = []  # (start_row, end_row)
        in_block = False
        block_start = 0
        empty_count = 0
        SCAN_COLS = 8  # Columns A-H (wider scan where data may be right-aligned)

        for r in range(1, max_row + 1):
            # Check data in columns A-H
            has_data = False
            for c in range(1, SCAN_COLS + 1):
                val = ws.cell_value(r, c)
                if val is not None and str(val).strip():
                    has_data = True
                    break

            # Check for CJK header row (operation title) in columns A-C
            is_cjk_header = False
            for c in range(1, 4):
                val = ws.cell_value(r, c)
                if val is not None and CJK_RE.search(str(val)):
                    is_cjk_header = True
                    break

            if has_data and not is_cjk_header:
                if not in_block:
                    in_block = True
                    block_start = r
                    empty_count = 0
                empty_count = 0
            else:
                if in_block:
                    empty_count += 1
                    # Use threshold 5 (consistent with _find_table_data_end)
                    # to avoid premature block splitting on instruction/image rows
                    if empty_count >= 5 or is_cjk_header:
                        # End of block
                        data_blocks.append((block_start, r - empty_count))
                        in_block = False
                        empty_count = 0

        # Close last block
        if in_block:
            data_blocks.append((block_start, max_row))

        # Filter: minimum 3 data rows in block
        for idx, (start, end) in enumerate(data_blocks):
            if end - start < 2:
                continue

            # Search for header above block
            header_row = start
            for r in range(max(1, start - 5), start):
                row_vals = []
                for c in range(1, 12):
                    v = ws.cell_value(r, c)
                    if v is not None:
                        row_vals.append(str(v).strip().lower())
                if any(
                    any(kw in rv for kw in HeuristicAnalyzer._get_part_no_keywords())
                    for rv in row_vals
                ):
                    header_row = r
                    break

            # Extract operation name
            op_name = ""
            for r in range(max(1, header_row - 3), header_row):
                for c in range(1, 8):
                    v = ws.cell_value(r, c)
                    if v is not None and CJK_RE.search(str(v)):
                        op_name = str(v).strip()
                        if len(op_name) > 3:
                            break
                if op_name:
                    break

            boundaries.append(
                TableBoundary(
                    header_row=header_row,
                    data_start=start,
                    data_end=end,
                    operation_name=op_name,
                    source_path=source_path,
                    sheet_name=sheet_name,
                    card_label=f"{idx + 1:03d}_{_safe_filename(op_name)[:30]}"
                    if op_name
                    else f"Op{idx + 1:03d}",
                )
            )

    finally:
        reader.close()

    return boundaries


def _cleanup_workbook_for_single_sheet(
    all_entries: dict[str, bytes],
    target_sheet_name: str,
) -> None:
    """Clean up workbook.xml, workbook.xml.rels for a single-sheet file.

    After vertical split, the output ZIP contains a workbook with only one sheet,
    but the workbook.xml may still reference other sheets, have wrong activeTab,
    high sheetId, etc. This function fixes those issues.

    Modifies all_entries in-place.
    """
    # First, find the target rId from the existing workbook.xml.rels
    target_rid = None
    wb_rels_bytes = all_entries.get("xl/_rels/workbook.xml.rels")
    if wb_rels_bytes is not None:
        if _HAS_LXML:
            try:
                rels_root = _lxml_etree.fromstring(wb_rels_bytes)
                # Find which rId maps to the worksheet that has our target sheet
                wb_bytes = all_entries.get("xl/workbook.xml")
                if wb_bytes is not None:
                    wb_root = _lxml_etree.fromstring(wb_bytes)
                    sheets = wb_root.find(f"{{{NS_MAIN}}}sheets")
                    if sheets is not None:
                        for sheet_el in sheets.findall(f"{{{NS_MAIN}}}sheet"):
                            if sheet_el.get("name") == target_sheet_name:
                                target_rid = sheet_el.get(f"{{{NS_R}}}id")
                                break
            except Exception:
                pass

    # Fix workbook.xml
    wb_bytes = all_entries.get("xl/workbook.xml")
    if wb_bytes is None:
        return

    if _HAS_LXML:
        wb_root = _lxml_etree.fromstring(wb_bytes)
        ns = NS_MAIN

        # Fix bookViews: activeTab=0, firstSheet=0
        book_views = wb_root.find(f"{{{ns}}}bookViews")
        if book_views is not None:
            wb_view = book_views.find(f"{{{ns}}}workbookView")
            if wb_view is not None:
                wb_view.set("activeTab", "0")
                wb_view.set("firstSheet", "0")

        # Fix sheets: keep only the target sheet, set sheetId=1
        sheets = wb_root.find(f"{{{ns}}}sheets")
        if sheets is not None:
            to_remove = []
            found_target = False
            for sheet_el in sheets.findall(f"{{{ns}}}sheet"):
                name = sheet_el.get("name", "")
                if name == target_sheet_name and not found_target:
                    sheet_el.set("sheetId", "1")
                    found_target = True
                else:
                    to_remove.append(sheet_el)
            for el in to_remove:
                sheets.remove(el)

        # Remove all definedName elements (they reference other sheets)
        defined_names = wb_root.find(f"{{{ns}}}definedNames")
        if defined_names is not None:
            wb_root.remove(defined_names)

        # Remove customWorkbookViews (can cause openpyxl parse errors)
        custom_views = wb_root.find(f"{{{ns}}}customWorkbookViews")
        if custom_views is not None:
            wb_root.remove(custom_views)

        all_entries["xl/workbook.xml"] = _lxml_etree.tostring(
            wb_root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    else:
        # Regex fallback
        wb_text = wb_bytes.decode("utf-8")
        wb_text = re.sub(r'activeTab="\d+"', 'activeTab="0"', wb_text)
        wb_text = re.sub(r'firstSheet="\d+"', 'firstSheet="0"', wb_text)

        def _replace_sheets(m: re.Match) -> str:
            content = m.group(2)
            kept = None
            for sh in re.finditer(r"<sheet[^>]*/>", content):
                name_m = re.search(r'name="([^"]+)"', sh.group(0))
                if name_m and name_m.group(1) == target_sheet_name:
                    kept = sh.group(0)
                    break
            if kept:
                return f"{m.group(1)}\n{kept}\n{m.group(3)}"
            return m.group(0)

        wb_text = re.sub(
            r"(<sheets[^>]*>)(.*?)(</sheets>)",
            _replace_sheets,
            wb_text,
            count=1,
            flags=re.DOTALL,
        )
        wb_text = re.sub(
            r"<definedNames[^>]*>.*?</definedNames>", "", wb_text, flags=re.DOTALL
        )
        wb_text = re.sub(
            r"<customWorkbookViews[^>]*>.*?</customWorkbookViews>",
            "",
            wb_text,
            flags=re.DOTALL,
        )
        all_entries["xl/workbook.xml"] = wb_text.encode("utf-8")

    # Fix workbook.xml.rels — keep only the target sheet + shared resources
    if wb_rels_bytes is not None:
        if _HAS_LXML:
            rels_root = _lxml_etree.fromstring(wb_rels_bytes)
            to_remove = []
            for rel in list(rels_root):
                rel_type = rel.get("Type", "")
                rid = rel.get("Id", "")
                if "worksheet" in rel_type and rid != target_rid:
                    to_remove.append(rel)
            for el in to_remove:
                rels_root.remove(el)
            all_entries["xl/_rels/workbook.xml.rels"] = _lxml_etree.tostring(
                rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
        else:
            rels_text = wb_rels_bytes.decode("utf-8")
            if target_rid:
                # Remove all worksheet relationships except the target
                def _filter_rels(m: re.Match) -> str:
                    rel_xml = m.group(0)
                    rid_m = re.search(r'Id="([^"]+)"', rel_xml)
                    if rid_m and rid_m.group(1) != target_rid:
                        if "worksheet" in rel_xml:
                            return ""
                    return rel_xml

                rels_text = re.sub(r"<Relationship\b[^>]*/>", _filter_rels, rels_text)
            all_entries["xl/_rels/workbook.xml.rels"] = rels_text.encode("utf-8")


def _cleanup_app_xml(all_entries: dict[str, bytes]) -> None:
    """Update docProps/app.xml to reflect 1 sheet."""
    app_bytes = all_entries.get("docProps/app.xml")
    if app_bytes is None:
        return

    if _HAS_LXML:
        try:
            root = _lxml_etree.fromstring(app_bytes)
            # Fix HeadingPairs: <vt:i4>4</vt:i4> → <vt:i4>1</vt:i4>
            for elem in root.iter():
                if elem.tag == f"{{{NS_R}}}i4" or elem.tag.endswith("}i4"):
                    if elem.text and elem.text.strip() == "4":
                        parent = elem.getparent()
                        if parent is not None:
                            gparent = parent.getparent()
                            if gparent is not None:
                                # Check if this is inside HeadingPairs
                                gparent_tag = _lxml_etree.QName(gparent.tag).localname
                                if gparent_tag == "vector":
                                    elem.text = "1"
            # Fix TitlesOfParts: keep only the first sheet name
            for vector in root.iter():
                if _lxml_etree.QName(vector.tag).localname == "vector":
                    children = list(vector)
                    # Check if this is TitlesOfParts (contains lpstr children)
                    if (
                        len(children) > 1
                        and _lxml_etree.QName(children[0].tag).localname == "lpstr"
                    ):
                        # This is TitlesOfParts — keep only first entry
                        for child in children[1:]:
                            vector.remove(child)
                        vector.set("size", "1")
            all_entries["docProps/app.xml"] = _lxml_etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
        except Exception:
            pass
    else:
        # Regex fallback
        app_text = app_bytes.decode("utf-8")
        # Replace the i4 in HeadingPairs
        app_text = re.sub(
            r"(<HeadingPairs>.*?<vt:i4>)\d+(</vt:i4>.*?</HeadingPairs>)",
            r"\g<1>1\2",
            app_text,
            count=1,
            flags=re.DOTALL,
        )
        # Fix TitlesOfParts vector size and remove extra entries
        app_text = re.sub(
            r'(<TitlesOfParts>.*?<vt:vector\s+)size="\d+"',
            r'\g<1>size="1"',
            app_text,
            count=1,
            flags=re.DOTALL,
        )
        # Remove all but first <vt:lpstr> in TitlesOfParts
        tp_match = re.search(
            r"(<TitlesOfParts>.*?<vt:vector[^>]*>)(.*?)(</vt:vector>)",
            app_text,
            re.DOTALL,
        )
        if tp_match:
            inner = tp_match.group(2)
            first_lpstr = re.search(r"<vt:lpstr>.*?</vt:lpstr>", inner, re.DOTALL)
            if first_lpstr:
                new_inner = first_lpstr.group(0)
                app_text = (
                    app_text[: tp_match.start(2)]
                    + new_inner
                    + app_text[tp_match.end(2) :]
                )
        all_entries["docProps/app.xml"] = app_text.encode("utf-8")


def _cleanup_custom_xml(all_entries: dict[str, bytes]) -> None:
    """Remove WPS-specific custom.xml metadata (large, causes issues)."""
    # Replace with minimal valid custom.xml
    minimal = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"'
        ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "</Properties>"
    )
    all_entries["docProps/custom.xml"] = minimal.encode("utf-8")


def _vertical_split_worker(
    source_path: str,
    output_dir: str,
    sheet_name: str,
    boundaries: list[TableBoundary],
    card_label: str,
    preloaded_zip: bytes | None = None,
) -> list[str]:
    """Split one sheet by vertical boundaries via ZIP manipulation.

    Preserves images, formatting and styles by working directly
    with the .xlsx ZIP structure (not via openpyxl Workbook).

    Algorithm for each operation:
      1. Copy the source .xlsx (one sheet, all images).
      2. Filter sheet XML: keep only <row> for the target range.
      3. Filter drawing XML: keep only anchors for the target range.
      4. Adjust row positions in anchors.
      5. Write the modified ZIP.

    Args:
        source_path: Path to the source .xlsx file (already one sheet).
        output_dir: Output directory.
        sheet_name: Sheet name.
        boundaries: List of table (operation) boundaries.
        card_label: Label for file naming.

    Returns:
        List of created file paths.
    """
    os.makedirs(output_path_dir := output_dir, exist_ok=True)
    created: list[str] = []
    _safe_filename(card_label)[:50] if card_label else ""

    # Read ZIP into memory
    if preloaded_zip is not None:
        zip_data = preloaded_zip
    else:
        try:
            with open(source_path, "rb") as f:
                zip_data = f.read()
        except OSError as e:
            logger.error("Cannot read source for vertical split: %s", e)
            return created

    # Read all ZIP entries once
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            all_entries: dict[str, bytes] = {}
            for name in zf.namelist():
                try:
                    all_entries[name] = zf.read(name)
                except zipfile.BadZipFile as e:
                    logger.warning("Skipping corrupt entry %s: %s", name, e)
    except zipfile.BadZipFile as e:
        logger.error("Cannot open source ZIP for vertical split: %s", e)
        return created

    set(all_entries.keys())

    # Find sheet name in workbook.xml to determine rId
    wb_xml_bytes = all_entries.get("xl/workbook.xml")
    if wb_xml_bytes is None:
        return created

    wb_root = ET.fromstring(wb_xml_bytes)
    sheets_elem = wb_root.find(f"{{{NS_MAIN}}}sheets")
    if sheets_elem is None:
        return created

    # rId → sheet name mapping
    target_r_id: str | None = None
    for sheet_el in sheets_elem.findall(f"{{{NS_MAIN}}}sheet"):
        if sheet_el.get("name") == sheet_name:
            target_r_id = sheet_el.get(f"{{{NS_R}}}id") or sheet_el.get("r:id")
            break

    if target_r_id is None:
        return created

    # rId → target path from workbook.xml.rels
    sheet_target: str | None = None
    rels_bytes = all_entries.get("xl/_rels/workbook.xml.rels")
    if rels_bytes is not None:
        rels_root = ET.fromstring(rels_bytes)
        for rel_el in rels_root:
            if rel_el.get("Id") == target_r_id and "worksheet" in rel_el.get(
                "Type", ""
            ):
                sheet_target = rel_el.get("Target", "").lstrip("/")
                if not sheet_target.startswith("xl/"):
                    sheet_target = "xl/" + sheet_target
                break

    if sheet_target is None:
        return created

    # Determine drawing XML for this sheet
    sheet_dir = os.path.dirname(sheet_target)
    sheet_base = os.path.basename(sheet_target)
    sheet_rels_path = f"{sheet_dir}/_rels/{sheet_base}.rels"
    drawing_path: str | None = None
    vml_path: str | None = None
    comments_path: str | None = None

    sr_bytes = all_entries.get(sheet_rels_path)
    if sr_bytes is not None:
        sr_root = ET.fromstring(sr_bytes)
        sr_base_dir = os.path.dirname(sheet_target)
        for sr_el in sr_root:
            target = sr_el.get("Target", "")
            rtype = sr_el.get("Type", "")
            resolved = os.path.normpath(os.path.join(sr_base_dir, target)).replace(
                os.sep, "/"
            )
            if "drawing" in rtype.lower() and "vml" not in rtype.lower():
                drawing_path = resolved
            elif "vml" in rtype.lower():
                vml_path = resolved
            elif "comment" in rtype.lower():
                comments_path = resolved

    # Read drawing XML as bytes (NOT via ET — preserve original namespaces)
    if drawing_path and drawing_path in all_entries:
        all_entries[drawing_path]

    safe_label_prefix = _safe_filename(card_label)[:50] if card_label else ""

    # ── Find ALL drawing files and their rels (WPS may bind image to wrong sheet) ──
    all_drawings: dict[str, bytes] = {}  # drawing_path -> raw bytes
    all_drawing_rels: dict[str, str] = {}  # drawing_rels_path -> drawing_path
    all_drawing_rels_map: dict[
        str, dict[str, str]
    ] = {}  # drawing_rels_path -> {rId -> media_path}

    for entry_name in list(all_entries.keys()):
        if (
            entry_name.startswith("xl/drawings/drawing")
            and entry_name.endswith(".xml")
            and "_rels" not in entry_name
        ):
            all_drawings[entry_name] = all_entries[entry_name]
            dr_path = f"{os.path.dirname(entry_name)}/_rels/{os.path.basename(entry_name)}.rels"
            all_drawing_rels[dr_path] = entry_name
            if dr_path in all_entries:
                rid_map: dict[str, str] = {}
                try:
                    dr_root = ET.fromstring(all_entries[dr_path])
                    for dr_el in dr_root:
                        rid = dr_el.get("Id", "")
                        target = dr_el.get("Target", "")
                        if rid and target:
                            resolved = os.path.normpath(
                                os.path.join(os.path.dirname(entry_name), target)
                            ).replace(os.sep, "/")
                            rid_map[rid] = resolved
                except Exception as e:
                    logger.debug("Failed to parse drawing rels %s: %s", dr_path, e)
                all_drawing_rels_map[dr_path] = rid_map

    for i, boundary in enumerate(boundaries):
        op_label = boundary.card_label or f"Op{i + 1:03d}"
        if safe_label_prefix:
            output_filename = (
                f"{safe_label_prefix}_{_safe_filename(op_label)[:40]}.xlsx"
            )
        else:
            output_filename = f"{_safe_filename(op_label)[:50]}.xlsx"

        output_path = os.path.join(output_dir, output_filename)

        # Post-split validation: skip empty operations
        if boundary.data_end <= boundary.header_row:
            logger.warning(
                "Skipping empty operation %d in %s",
                i + 1,
                os.path.basename(source_path),
            )
            continue

        # ── Filter ALL drawing XML and collect retained rIds ──
        filtered_drawings: dict[
            str, bytes | None
        ] = {}  # drawing_path -> filtered bytes (None = skip)
        retained_image_paths: set[str] = set()
        current_retained_rids: set[str] = set()
        comments_fully_removed = False  # True if all comments are outside the range
        if comments_path and comments_path in all_entries:
            test_filtered = _filter_comments_xml(
                all_entries[comments_path], boundary.header_row, boundary.data_end
            )
            if test_filtered is None:
                comments_fully_removed = True

        for dr_path, dr_bytes in all_drawings.items():
            # Drawing XML uses 0-indexed rows (row 0 = Excel row 1)
            # Boundary header_row/data_end are 1-indexed (Excel rows)
            # Convert: subtract 1 for drawing filter
            filtered, rids = _filter_drawing_xml_with_rids(
                dr_bytes, boundary.header_row - 1, boundary.data_end - 1
            )
            filtered_drawings[dr_path] = filtered
            if filtered is not None:
                current_retained_rids.update(rids)
                # Map retained rIds → media paths
                # all_drawing_rels_map is keyed by rels path, not drawing path
                dr_rels_path = (
                    f"{os.path.dirname(dr_path)}/_rels/{os.path.basename(dr_path)}.rels"
                )
                rid_map = all_drawing_rels_map.get(dr_rels_path, {})
                for rid in rids:
                    media_path = rid_map.get(rid, "")
                    if media_path:
                        retained_image_paths.add(media_path)
                logger.debug(
                    "Drawing %s: filtered %d bytes -> %d bytes, rids=%s",
                    dr_path,
                    len(dr_bytes),
                    len(filtered),
                    rids,
                )
            else:
                logger.debug("Drawing %s: fully outside range, skipping", dr_path)

        try:
            # Apply workbook/docProps cleanup in-place before writing
            _cleanup_workbook_for_single_sheet(all_entries, sheet_name)
            _cleanup_app_xml(all_entries)
            _cleanup_custom_xml(all_entries)

            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf_write:
                for name, data in all_entries.items():
                    # Skip unused sheet XML files (keep only the target sheet)
                    if (
                        name.startswith("xl/worksheets/sheet")
                        and name.endswith(".xml")
                        and "_rels" not in name
                        and name != sheet_target
                    ):
                        continue
                    # Skip unused sheet .rels files
                    if (
                        name.startswith("xl/worksheets/_rels/sheet")
                        and name.endswith(".rels")
                        and name != sheet_rels_path
                    ):
                        continue
                    # Skip unused sheet comments
                    if (
                        name.startswith("xl/comments")
                        and name.endswith(".xml")
                        and name != comments_path
                    ):
                        continue
                    if name == sheet_target:
                        data = _filter_sheet_xml(
                            data, boundary.header_row, boundary.data_end
                        )
                    elif (
                        name in filtered_drawings
                        and filtered_drawings[name] is not None
                    ):
                        data = filtered_drawings[name]
                    elif name in filtered_drawings and filtered_drawings[name] is None:
                        continue  # Drawing fully outside keep range — skip
                    elif (
                        name in all_drawing_rels
                        and filtered_drawings.get(all_drawing_rels[name]) is not None
                    ):
                        all_drawing_rels[name]
                        rids_for_dr = set()
                        # all_drawing_rels_map is keyed by rels path (name), not drawing path
                        for r, p in all_drawing_rels_map.get(name, {}).items():
                            if p in retained_image_paths:
                                rids_for_dr.add(r)
                        data = _filter_drawing_rels(data, rids_for_dr)
                    elif (
                        name in all_drawing_rels
                        and filtered_drawings.get(all_drawing_rels[name]) is None
                    ):
                        continue  # Parent drawing fully outside — skip rels too
                    elif name == vml_path and vml_path is not None:
                        data = _filter_vml_xml(
                            data, boundary.header_row, boundary.data_end
                        )
                    elif (
                        comments_path
                        and name == comments_path
                        and comments_path is not None
                    ):
                        filtered_comments = _filter_comments_xml(
                            data, boundary.header_row, boundary.data_end
                        )
                        if filtered_comments is None:
                            continue  # All comments outside range — skip
                        data = filtered_comments

                    # ── Remove comments reference from sheet .rels ──
                    if (
                        comments_fully_removed
                        and name == sheet_rels_path
                        and comments_path
                    ):
                        try:
                            rels_root_cleanup = ET.fromstring(data)
                            to_drop = []
                            for rel_el in rels_root_cleanup:
                                target = rel_el.get("Target", "")
                                resolved = os.path.normpath(
                                    os.path.join(os.path.dirname(sheet_target), target)
                                ).replace(os.sep, "/")
                                if resolved == comments_path:
                                    to_drop.append(rel_el)
                            for el in to_drop:
                                rels_root_cleanup.remove(el)
                            data = _serialize_xml(rels_root_cleanup, NS_PKG_RELS)
                        except ET.ParseError:
                            pass

                    # ── Remove comments Override from Content_Types ──
                    if (
                        comments_fully_removed
                        and name == "[Content_Types].xml"
                        and comments_path
                    ):
                        ct_str = data.decode("utf-8", errors="replace")
                        ct_str = re.sub(
                            r'<Override[^>]*PartName="[^"]*comment[^"]*"[^>]*/>',
                            "",
                            ct_str,
                        )
                        ct_str = re.sub(
                            r'<Override[^>]*PartName="[^"]*Comment[^"]*"[^>]*/>',
                            "",
                            ct_str,
                        )
                        data = ct_str.encode("utf-8")

                    # ── Media filtering: skip unused images ──
                    # SAFE STRATEGY: copy all media by default.
                    # Filter ONLY if:
                    #   1. all_drawing_rels_map is not empty (.rels parsed successfully)
                    #   2. retained_image_paths is not empty (anchors exist in range)
                    # If either condition fails — copy all media.
                    # This prevents image loss when data is incomplete.
                    any_rels_parsed = any(
                        rid_map for rid_map in all_drawing_rels_map.values()
                    )
                    should_filter_media = any_rels_parsed and retained_image_paths
                    if name.startswith("xl/media/"):
                        if should_filter_media:
                            if name not in retained_image_paths:
                                logger.debug(
                                    "Filtered media: %s (not in retained set)",
                                    name,
                                )
                                continue  # Skip unused image
                        # else: copy all media (safe default)

                    zf_write.writestr(name, data)

        except zipfile.BadZipFile as e:
            logger.error("Failed to create vertical split %s: %s", output_filename, e)
            continue

        # Validate the output file
        if not _validate_split_file(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
            logger.warning("Deleted invalid split file: %s", output_filename)
            continue

        created.append(output_path)
        logger.info(
            "Vertical split (ZIP): operation %d '%s' [%d-%d] → %s",
            i + 1,
            boundary.operation_name[:30] or "",
            boundary.header_row,
            boundary.data_end,
            os.path.basename(output_path),
        )

    return created


def _filter_sheet_xml(
    sheet_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> bytes:
    """Filter sheet XML — keep only rows in range with reindexed references.

    Uses lxml for proper namespace handling and valid XML output.
    Falls back to regex when lxml is unavailable.
    """
    if _HAS_LXML:
        return _filter_sheet_xml_lxml(sheet_data, keep_from_row, keep_to_row)
    return _filter_sheet_xml_regex(sheet_data, keep_from_row, keep_to_row)


def _reindex_cell_ref(col_letters: str, old_row: int, keep_from_row: int) -> str:
    """Reindex a cell reference: B4 with keep_from_row=4 → B1."""
    new_row = max(1, old_row - keep_from_row + 1)
    return f"{col_letters}{new_row}"


def _reindex_range_ref(ref: str, keep_from_row: int, keep_to_row: int) -> str | None:
    """Reindex a range reference like B4:D10, keeping only the intersection with [keep_from_row, keep_to_row].

    Returns None if the range is completely outside the keep range.
    """
    if ":" not in ref:
        # Single cell
        m = re.match(r"^([A-Z]+)(\d+)$", ref)
        if not m:
            return ref
        row = int(m.group(2))
        if row < keep_from_row or row > keep_to_row:
            return None
        return _reindex_cell_ref(m.group(1), row, keep_from_row)

    parts = ref.split(":")
    m1 = re.match(r"^([A-Z]+)(\d+)$", parts[0])
    m2 = re.match(r"^([A-Z]+)(\d+)$", parts[1])
    if not m1 or not m2:
        return ref

    r1 = int(m1.group(2))
    r2 = int(m2.group(2))
    c1 = m1.group(1)
    c2 = m2.group(1)

    if r2 < keep_from_row or r1 > keep_to_row:
        return None

    new_r1 = max(1, max(r1, keep_from_row) - keep_from_row + 1)
    new_r2 = max(1, min(r2, keep_to_row) - keep_from_row + 1)
    return f"{c1}{new_r1}:{c2}{new_r2}"


def _filter_sheet_xml_lxml(
    sheet_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> bytes:
    """lxml-based sheet XML filter."""
    root = _lxml_etree.fromstring(sheet_data)
    ns = NS_MAIN
    _cell_ref_re = re.compile(r"^([A-Z]+)(\d+)$")

    # Register namespaces for proper output (skip empty prefix — lxml rejects it)
    for prefix, uri in _LXML_NS.items():
        if prefix:
            _lxml_etree.register_namespace(prefix, uri)

    # 1. Update <dimension>
    dim = root.find(f"{{{ns}}}dimension")
    if dim is not None:
        ref = dim.get("ref", "")
        try:
            _, _, max_col, _ = range_boundaries(ref)
        except (ValueError, IndexError):
            max_col = 10
        new_count = keep_to_row - keep_from_row + 1
        dim.set("ref", f"A1:{get_column_letter(max_col)}{new_count}")

    # 2. Filter <sheetData>/<row> and reindex cell references
    sheet_data_elem = root.find(f"{{{ns}}}sheetData")
    if sheet_data_elem is not None:
        rows_to_remove = []
        for row_elem in sheet_data_elem.findall(f"{{{ns}}}row"):
            r_attr = row_elem.get("r")
            if r_attr is None:
                rows_to_remove.append(row_elem)
                continue
            try:
                r = int(r_attr)
            except ValueError:
                rows_to_remove.append(row_elem)
                continue

            if r < keep_from_row or r > keep_to_row:
                rows_to_remove.append(row_elem)
                continue

            new_r = r - keep_from_row + 1
            row_elem.set("r", str(new_r))

            # Reindex cell r attributes
            for c_elem in row_elem.findall(f"{{{ns}}}c"):
                cell_ref = c_elem.get("r", "")
                m = _cell_ref_re.match(cell_ref)
                if m:
                    c_elem.set(
                        "r",
                        _reindex_cell_ref(m.group(1), int(m.group(2)), keep_from_row),
                    )

        for elem in rows_to_remove:
            sheet_data_elem.remove(elem)

    # 3. Filter <mergeCells>
    merge_cells = root.find(f"{{{ns}}}mergeCells")
    if merge_cells is not None:
        to_remove = []
        kept_count = 0
        for mc in merge_cells.findall(f"{{{ns}}}mergeCell"):
            ref = mc.get("ref", "")
            try:
                min_col, min_r, max_col, max_r = range_boundaries(ref)
            except (ValueError, IndexError):
                to_remove.append(mc)
                continue
            if max_r < keep_from_row or min_r > keep_to_row:
                to_remove.append(mc)
                continue
            nr1 = max(min_r, keep_from_row) - keep_from_row + 1
            nr2 = min(max_r, keep_to_row) - keep_from_row + 1
            mc.set(
                "ref",
                f"{get_column_letter(min_col)}{nr1}:{get_column_letter(max_col)}{nr2}",
            )
            kept_count += 1
        for elem in to_remove:
            merge_cells.remove(elem)
        merge_cells.set("count", str(kept_count))
        if kept_count == 0:
            root.remove(merge_cells)

    # 4. Remove autoFilter
    for af in root.findall(f"{{{ns}}}autoFilter"):
        root.remove(af)

    # Remove filterMode from sheetPr
    sheet_pr = root.find(f"{{{ns}}}sheetPr")
    if sheet_pr is not None:
        if "filterMode" in sheet_pr.attrib:
            del sheet_pr.attrib["filterMode"]

    # Remove extLst (references features that become invalid after reindex)
    for ext_lst in root.findall(f"{{{ns}}}extLst"):
        root.remove(ext_lst)

    # Also remove extLst inside sheetPr
    if sheet_pr is not None:
        for ext_lst in sheet_pr.findall(f"{{{ns}}}extLst"):
            sheet_pr.remove(ext_lst)

    # 5. Remove dataValidations
    for dv in root.findall(f"{{{ns}}}dataValidations"):
        root.remove(dv)

    # 5b. Filter conditionalFormatting
    for cf in list(root.findall(f"{{{ns}}}conditionalFormatting")):
        sqref = cf.get("sqref", "")
        parts = re.split(r"\s+", sqref)
        kept_parts = []
        for part in parts:
            reindexed = _reindex_range_ref(part, keep_from_row, keep_to_row)
            if reindexed is not None:
                kept_parts.append(reindexed)
        if not kept_parts:
            root.remove(cf)
        else:
            cf.set("sqref", " ".join(kept_parts))

    # 6. Reset sheetView
    for sv in root.findall(f"{{{ns}}}sheetView"):
        sv.set("topLeftCell", "A1")
        if sv.get("view") == "pageBreakPreview":
            del sv.attrib["view"]
        # Reset selection
        for sel in sv.findall(f"{{{ns}}}selection"):
            sel.set("activeCell", "A1")
            sel.set("sqref", "A1")

    # 7. Filter rowBreaks
    for rb in list(root.findall(f"{{{ns}}}rowBreaks")):
        to_remove = []
        for brk in rb.findall(f"{{{ns}}}brk"):
            brk_id = brk.get("id")
            if brk_id is None:
                to_remove.append(brk)
                continue
            try:
                br = int(brk_id)
            except ValueError:
                to_remove.append(brk)
                continue
            if br < keep_from_row or br > keep_to_row:
                to_remove.append(brk)
            else:
                brk.set("id", str(max(1, br - keep_from_row + 1)))
        for elem in to_remove:
            rb.remove(elem)
        if len(rb) == 0:
            root.remove(rb)

    return _lxml_etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _filter_sheet_xml_regex(
    sheet_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> bytes:
    """Regex fallback for _filter_sheet_xml when lxml is unavailable."""
    xml_text = sheet_data.decode("utf-8")

    def _update_dim(m: re.Match) -> str:
        full = m.group(0)
        ref = m.group(1)
        try:
            _, _, max_col, _ = range_boundaries(ref)
        except (ValueError, IndexError):
            max_col = 10
        new_count = keep_to_row - keep_from_row + 1
        return re.sub(
            r'ref="[^"]*"',
            f'ref="A1:{get_column_letter(max_col)}{new_count}"',
            full,
        )

    xml_text = re.sub(
        r'<[^>]*dimension[^>]*ref="([^"]+)"[^>]*/?\s*>',
        _update_dim,
        xml_text,
        count=1,
    )

    _CELL_REF_RE = re.compile(r'(r=")([A-Za-z]+)(\d+)(")')

    def _filter_sd(m: re.Match) -> str:
        sd_open = m.group(1)
        sd_content = m.group(2)
        sd_close = m.group(3)

        kept_rows = []
        for row_m in re.finditer(
            r"(<(?:[\w\-]+:)?row\b[^>]*>.*?</(?:[\w\-]+:)?row>)",
            sd_content,
            re.DOTALL,
        ):
            row_xml = row_m.group(0)
            r_match = re.search(r'\br="(\d+)"', row_xml)
            if not r_match:
                continue
            r = int(r_match.group(1))
            if r < keep_from_row or r > keep_to_row:
                continue

            new_r = r - keep_from_row + 1
            row_xml = re.sub(
                r'(\br=")\d+(")',
                lambda m, nr=new_r: f"{m.group(1)}{nr}{m.group(2)}",
                row_xml,
            )

            def _upd_cref(cm: re.Match, nr=new_r) -> str:
                return f"{cm.group(1)}{cm.group(2)}{nr}{cm.group(4)}"

            row_xml = _CELL_REF_RE.sub(_upd_cref, row_xml)
            kept_rows.append(row_xml)

        return sd_open + "".join(kept_rows) + sd_close

    xml_text = re.sub(
        r"(<(?:[\w\-]+:)?sheetData[^>]*>)(.*?)(</(?:[\w\-]+:)?sheetData>)",
        _filter_sd,
        xml_text,
        flags=re.DOTALL,
    )

    def _filter_mc(m: re.Match) -> str:
        mc_open = m.group(1)
        mc_content = m.group(2)
        mc_close = m.group(3)

        kept_mcs = []
        for cell_m in re.finditer(r"<(?:[\w\-]+:)?mergeCell[^/]*/>", mc_content):
            cx = cell_m.group(0)
            ref_m = re.search(r'ref="([^"]+)"', cx)
            if not ref_m:
                continue
            ref = ref_m.group(1)
            parts = ref.split(":")
            if len(parts) != 2:
                continue
            try:
                min_col, min_r, max_col, max_r = range_boundaries(ref)
            except (ValueError, IndexError):
                continue
            if max_r < keep_from_row or min_r > keep_to_row:
                continue
            nr1 = max(min_r, keep_from_row) - keep_from_row + 1
            nr2 = min(max_r, keep_to_row) - keep_from_row + 1
            new_ref = (
                f"{get_column_letter(min_col)}{nr1}:{get_column_letter(max_col)}{nr2}"
            )
            cx = re.sub(r'ref="[^"]*"', f'ref="{new_ref}"', cx)
            kept_mcs.append(cx)

        if not kept_mcs:
            return ""
        mc_open = re.sub(r'count="\d+"', f'count="{len(kept_mcs)}"', mc_open)
        return mc_open + "".join(kept_mcs) + mc_close

    xml_text = re.sub(
        r"(<(?:[\w\-]+:)?mergeCells[^>]*>)(.*?)(</(?:[\w\-]+:)?mergeCells>)",
        _filter_mc,
        xml_text,
        flags=re.DOTALL,
    )

    xml_text = re.sub(r"<(?:[\w\-]+:)?autoFilter[^>]*/>\s*", "", xml_text)
    xml_text = re.sub(
        r"<(?:[\w\-]+:)?autoFilter[^>]*>.*?</(?:[\w\-]+:)?autoFilter>\s*",
        "",
        xml_text,
        flags=re.DOTALL,
    )
    xml_text = re.sub(r'(\bsheetPr[^>]*?)\s+filterMode="[^"]*"', r"\1", xml_text)
    xml_text = re.sub(
        r"<(?:[\w\-]+:)?extLst[^>]*>.*?</(?:[\w\-]+:)?extLst>\s*",
        "",
        xml_text,
        flags=re.DOTALL,
    )

    xml_text = re.sub(r"<(?:[\w\-]+:)?dataValidations[^>]*/>\s*", "", xml_text)
    xml_text = re.sub(
        r"<(?:[\w\-]+:)?dataValidations[^>]*>.*?</(?:[\w\-]+:)?dataValidations>\s*",
        "",
        xml_text,
        flags=re.DOTALL,
    )

    def _filter_cond_fmt(m: re.Match) -> str:
        cf_text = m.group(0)
        sqref_m = re.search(r'sqref="([^"]+)"', cf_text)
        if not sqref_m:
            return cf_text
        sqref = sqref_m.group(1)
        parts = re.split(r"\s+", sqref)
        kept_parts = []
        for part in parts:
            if ":" in part:
                try:
                    _, min_r, _, max_r = range_boundaries(part)
                    if min_r <= keep_to_row and max_r >= keep_from_row:
                        kept_parts.append(part)
                except (ValueError, IndexError):
                    kept_parts.append(part)
            else:
                cell_match = re.match(r"^([A-Z]+)(\d+)$", part)
                if cell_match:
                    row_num = int(cell_match.group(2))
                    if keep_from_row <= row_num <= keep_to_row:
                        kept_parts.append(part)
                else:
                    kept_parts.append(part)
        if not kept_parts:
            return ""
        new_sqref = " ".join(kept_parts)
        return re.sub(r'sqref="[^"]*"', f'sqref="{new_sqref}"', cf_text)

    xml_text = re.sub(
        r"<(?:[\w\-]+:)?conditionalFormatting[^>]*>.*?</(?:[\w\-]+:)?conditionalFormatting>",
        _filter_cond_fmt,
        xml_text,
        flags=re.DOTALL,
    )
    xml_text = re.sub(
        r"<(?:[\w\-]+:)?conditionalFormatting[^/]*/>\s*",
        _filter_cond_fmt,
        xml_text,
    )

    def _reset_sheetview(m: re.Match) -> str:
        sv = m.group(0)
        sv = re.sub(r'topLeftCell="[A-Z]+\d+"', 'topLeftCell="A1"', sv)
        sv = re.sub(r' ?view="pageBreakPreview"', "", sv)
        return sv

    xml_text = re.sub(
        r"<(?:[\w\-]+:)?sheetView\b[^>]*/>",
        _reset_sheetview,
        xml_text,
    )
    xml_text = re.sub(
        r"<(?:[\w\-]+:)?sheetView\b[^>]*>.*?</(?:[\w\-]+:)?sheetView>",
        _reset_sheetview,
        xml_text,
        flags=re.DOTALL,
    )

    def _reset_selection(m: re.Match) -> str:
        sel = m.group(0)
        sel = re.sub(r'activeCell="[A-Z]+\d+"', 'activeCell="A1"', sel)
        sel = re.sub(r'sqref="[A-Z]+\d+(?::[A-Z]+\d+)?"', 'sqref="A1"', sel)
        return sel

    xml_text = re.sub(r"<(?:[\w\-]+:)?selection\b[^>]*/>", _reset_selection, xml_text)

    def _filter_breaks(m: re.Match) -> str:
        bk_open = m.group(1)
        bk_content = m.group(2)
        bk_close = m.group(3)
        kept_brs = []
        for br_m in re.finditer(r"<(?:[\w\-]+:)?brk[^>]*/>", bk_content):
            bx = br_m.group(0)
            id_m = re.search(r'id="(\d+)"', bx)
            if not id_m:
                continue
            br_r = int(id_m.group(1))
            if br_r < keep_from_row or br_r > keep_to_row:
                continue
            new_br_r = br_r - keep_from_row + 1
            bx = re.sub(r'id="\d+"', f'id="{new_br_r}"', bx)
            kept_brs.append(bx)
        if not kept_brs:
            return ""
        return bk_open + "".join(kept_brs) + bk_close

    xml_text = re.sub(
        r"(<(?:[\w\-]+:)?rowBreaks[^>]*>)(.*?)(</(?:[\w\-]+:)?rowBreaks>)",
        _filter_breaks,
        xml_text,
        flags=re.DOTALL,
    )

    return xml_text.encode("utf-8")


def _filter_drawing_xml(
    drawing_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> bytes:
    """Filter drawing XML, keeping only anchors in the target range.

    Uses regex-based string operations instead of ET.fromstring/ET.tostring
    to preserve original namespace declarations (xmlns:ns2, xmlns:ns4
    etc.) which Python ET may rename during serialization,
    causing the Excel error "Repaired Records: Drawing shape".

    Supports twoCellAnchor, oneCellAnchor and absoluteAnchor.
    Adjusts anchor row positions.
    """
    result, _ = _filter_drawing_xml_with_rids(
        drawing_data,
        keep_from_row,
        keep_to_row,
    )
    return result


def _filter_drawing_xml_with_rids(
    drawing_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> tuple[bytes, set[str]]:
    """Filter drawing XML using lxml and return retained image rIds.

    Uses proper XML DOM parsing instead of regex to produce valid OOXML.
    Handles twoCellAnchor, oneCellAnchor, and absoluteAnchor elements.
    Collects r:embed rIds from retained <a:blip> elements.

    Returns:
        Tuple of (filtered_xml_bytes, retained_rids).
    """
    retained_rids: set[str] = set()

    if _HAS_LXML:
        return _filter_drawing_xml_lxml(
            drawing_data, keep_from_row, keep_to_row, retained_rids
        )

    # Fallback: regex-based (original code)
    return _filter_drawing_xml_regex(
        drawing_data, keep_from_row, keep_to_row, retained_rids
    )


def _filter_drawing_xml_lxml(
    drawing_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
    retained_rids: set[str],
) -> tuple[bytes, set[str]]:
    """lxml-based drawing XML filter — produces valid OOXML output."""
    root = _lxml_etree.fromstring(drawing_data)

    # Register namespaces so lxml uses proper prefixes when serializing
    for prefix, uri in _LXML_NS.items():
        _lxml_etree.register_namespace(prefix, uri)

    ns_xdr = NS_DRAWING
    ns_a = NS_DRAWINGML
    ns_r = NS_R

    to_remove = []

    for anchor in root:
        tag = _lxml_etree.QName(anchor.tag).localname

        if tag in ("twoCellAnchor", "oneCellAnchor"):
            from_elem = anchor.find(f"{{{ns_xdr}}}from")
            to_elem = anchor.find(f"{{{ns_xdr}}}to")

            from_row = 0
            to_row = 0

            if from_elem is not None:
                row_elem = from_elem.find(f"{{{ns_xdr}}}row")
                if row_elem is not None and row_elem.text:
                    from_row = int(row_elem.text)

            if to_elem is not None:
                row_elem = to_elem.find(f"{{{ns_xdr}}}row")
                if row_elem is not None and row_elem.text:
                    to_row = int(row_elem.text)

            # Remove anchors completely outside the range
            if to_row < keep_from_row or from_row > keep_to_row:
                to_remove.append(anchor)
                continue

            # Remove anchors whose CENTER is outside the range
            # (prevents bleeding from adjacent operations)
            center_row = (from_row + to_row) / 2.0
            if center_row < keep_from_row or center_row > keep_to_row:
                to_remove.append(anchor)
                continue

            # Clamp from/to to the keep range, then reindex
            clamped_from = max(from_row, keep_from_row)
            clamped_to = min(to_row, keep_to_row)

            if from_elem is not None:
                row_elem = from_elem.find(f"{{{ns_xdr}}}row")
                if row_elem is not None:
                    row_elem.text = str(clamped_from - keep_from_row)
                # Reset rowOff to 0 when clamping from_row
                if from_row < keep_from_row:
                    row_off = from_elem.find(f"{{{ns_xdr}}}rowOff")
                    if row_off is not None:
                        row_off.text = "0"

            if to_elem is not None:
                row_elem = to_elem.find(f"{{{ns_xdr}}}row")
                if row_elem is not None:
                    row_elem.text = str(clamped_to - keep_from_row)

            # Collect rIds from <a:blip r:embed="..."> elements
            for blip in anchor.iter(f"{{{ns_a}}}blip"):
                embed = blip.get(f"{{{ns_r}}}embed")
                if embed:
                    retained_rids.add(embed)

        elif tag == "absoluteAnchor":
            to_remove.append(anchor)

    for elem in to_remove:
        root.remove(elem)

    # Renumber all cNvPr ids sequentially (WPS generates huge ids like 101820)
    id_counter = 0
    for cnvpr in root.iter():
        if _lxml_etree.QName(cnvpr.tag).localname == "cNvPr":
            id_counter += 1
            cnvpr.set("id", str(id_counter))

    result_bytes = _lxml_etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    return result_bytes, retained_rids


def _filter_drawing_xml_regex(
    drawing_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
    retained_rids: set[str],
) -> tuple[bytes, set[str]]:
    """Regex-based fallback for when lxml is unavailable."""
    xml_text = drawing_data.decode("utf-8")

    _ANCHOR_TAG_RE = re.compile(
        r"<(?:[\w\-]+:)?(?:two|one)CellAnchor\b[^>]*>"
        r".*?"
        r"</(?:[\w\-]+:)?(?:two|one)CellAnchor>",
        re.DOTALL,
    )
    _ABS_ANCHOR_RE = re.compile(
        r"<(?:[\w\-]+:)?absoluteAnchor\b[^>]*>"
        r".*?"
        r"</(?:[\w\-]+:)?absoluteAnchor>",
        re.DOTALL,
    )

    def _filter_anchor(m: re.Match) -> str:
        anchor_xml = m.group(0)

        from_row = 0
        from_m = re.search(
            r"<(?:[\w\-]+:)?from\b[^>]*>(.*?)</(?:[\w\-]+:)?from>",
            anchor_xml,
            re.DOTALL,
        )
        if from_m:
            row_m = re.search(
                r"<(?:[\w\-]+:)?row>(\d+)</(?:[\w\-]+:)?row>",
                from_m.group(1),
            )
            if row_m:
                from_row = int(row_m.group(1))

        to_row = from_row
        to_m = re.search(
            r"<(?:[\w\-]+:)?to\b[^>]*>(.*?)</(?:[\w\-]+:)?to>",
            anchor_xml,
            re.DOTALL,
        )
        if to_m:
            row_m = re.search(
                r"<(?:[\w\-]+:)?row>(\d+)</(?:[\w\-]+:)?row>",
                to_m.group(1),
            )
            if row_m:
                to_row = int(row_m.group(1))

        if to_row < keep_from_row or from_row > keep_to_row:
            return ""

        def _update_row(row_m_inner: re.Match) -> str:
            tag_open = row_m_inner.group(1)
            val = int(row_m_inner.group(2))
            tag_close = row_m_inner.group(3)
            new_val = max(0, val - keep_from_row)
            return f"{tag_open}{new_val}{tag_close}"

        anchor_xml = re.sub(
            r"(<(?:[\w\-]+:)?row>)(\d+)(</(?:[\w\-]+:)?row>)",
            _update_row,
            anchor_xml,
        )

        for blip_m in re.finditer(
            r"<(?:[\w\-]+:)?blip\b[^>]*>",
            anchor_xml,
        ):
            embed_m = re.search(
                r'r:embed="([^"]+)"',
                blip_m.group(0),
            )
            if embed_m:
                retained_rids.add(embed_m.group(1))

        return anchor_xml

    xml_text = _ANCHOR_TAG_RE.sub(_filter_anchor, xml_text)

    def _filter_absolute(m: re.Match) -> str:
        if keep_from_row > 0:
            return ""
        return m.group(0)

    xml_text = _ABS_ANCHOR_RE.sub(_filter_absolute, xml_text)

    _id_counter = [0]

    def _renumber_id(m: re.Match) -> str:
        _id_counter[0] += 1
        return f"{m.group(1)}{_id_counter[0]}{m.group(3)}"

    xml_text = re.sub(
        r'(id=")(\d+)(")',
        _renumber_id,
        xml_text,
    )

    return xml_text.encode("utf-8"), retained_rids


def _filter_drawing_rels(
    rels_data: bytes,
    retained_rids: set[str],
) -> bytes:
    """Filter drawing .rels, keeping only retained rIds.

    Removes <Relationship> entries whose Id is not in retained_rids.
    Uses lxml for valid XML output.
    """
    if _HAS_LXML:
        root = _lxml_etree.fromstring(rels_data)
        to_remove = []
        for rel in root:
            rid = rel.get("Id", "")
            if rid and rid not in retained_rids:
                to_remove.append(rel)
        for elem in to_remove:
            root.remove(elem)
        return _lxml_etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # Regex fallback
    rels_text = rels_data.decode("utf-8")

    def _filter_rel(m: re.Match) -> str:
        rel_xml = m.group(0)
        id_m = re.search(r'Id="([^"]+)"', rel_xml)
        if id_m and id_m.group(1) not in retained_rids:
            return ""
        return rel_xml

    return re.sub(r"<Relationship\b[^>]*/>", _filter_rel, rels_text).encode("utf-8")


def _filter_vml_xml(
    vml_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> bytes:
    """Filter VML XML, removing shapes outside the row range.

    Uses lxml for proper namespace handling.
    VML shapes use two positioning modes:
      1. row attribute (direct row binding)
      2. CSS style="position:absolute;top:..." (approximate)
    """
    if _HAS_LXML:
        try:
            root = _lxml_etree.fromstring(vml_data)
        except Exception:
            return vml_data
    else:
        try:
            root = ET.fromstring(vml_data)
        except ET.ParseError:
            return vml_data

    vml_ns = "urn:schemas-microsoft-com:vml"
    office_ns = "urn:schemas-microsoft-com:office:office"
    ROW_HEIGHT_PT = 15

    to_remove = []
    for elem in root.iter(f"{{{vml_ns}}}shape"):
        row_attr = elem.get("row") or elem.get(f"{{{office_ns}}}row")
        if row_attr is not None:
            try:
                row_num = int(row_attr.split()[0])
                if row_num < keep_from_row or row_num > keep_to_row:
                    to_remove.append(elem)
                    continue
            except (ValueError, IndexError):
                pass

        style = elem.get("style", "")
        if "top:" in style.lower() or "top: " in style.lower():
            try:
                top_match = re.search(
                    r"top:\s*([\d.]+)\s*(?:pt|mm|cm)?", style, re.IGNORECASE
                )
                if top_match:
                    top_pt = float(top_match.group(1))
                    approx_row = int(top_pt / ROW_HEIGHT_PT) + 1
                    if approx_row < keep_from_row or approx_row > keep_to_row:
                        to_remove.append(elem)
                        continue
            except (ValueError, IndexError):
                pass

    for elem in to_remove:
        for parent in root.iter():
            if elem in list(parent):
                parent.remove(elem)
                break

    if _HAS_LXML:
        return _lxml_etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    return _serialize_xml(root, NS_MAIN, extra_ns={"v": VML_NS, "o": OFFICE_NS})


def _filter_comments_xml(
    comments_data: bytes,
    keep_from_row: int,
    keep_to_row: int,
) -> bytes | None:
    """Filter xl/commentsN.xml — keep and reindex comments in row range.

    Comments whose ref falls within [keep_from_row, keep_to_row] are kept
    with reindexed row numbers.

    Returns:
        Filtered XML bytes, or None if no comments in range.
    """
    if _HAS_LXML:
        try:
            root = _lxml_etree.fromstring(comments_data)
        except Exception:
            return comments_data
    else:
        try:
            root = ET.fromstring(comments_data)
        except ET.ParseError:
            return comments_data

    ns = NS_MAIN
    comment_list = root.find(f"{{{ns}}}commentList")
    if comment_list is None:
        return comments_data

    to_remove = []
    for comment_el in comment_list.findall(f"{{{ns}}}comment"):
        ref = comment_el.get("ref", "")
        ref_match = re.match(r"R(\d+)(.*)", ref)
        if ref_match:
            row_num = int(ref_match.group(1))
            suffix = ref_match.group(2)
            if row_num < keep_from_row or row_num > keep_to_row:
                to_remove.append(comment_el)
            else:
                new_row = row_num - keep_from_row + 1
                comment_el.set("ref", f"R{new_row}{suffix}")

    for elem in to_remove:
        comment_list.remove(elem)

    if len(comment_list) == 0:
        return None

    if _HAS_LXML:
        return _lxml_etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )
    return _serialize_xml(root, NS_MAIN)
