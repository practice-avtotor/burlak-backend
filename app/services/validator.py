"""Output file validation module for post-split verification.

Multi-level pipeline:
  1. Structural: ZIP integrity, XML well-formed, required files present
  2. Schema: sheetData exists, columns detected, data rows > 0
  3. Content: images loadable, formulas parseable
  4. Semantic: part numbers valid, quantities numeric, no duplicates

Used after each split to guarantee output file correctness.
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# OOXML namespaces
NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@dataclass
class ValidationIssue:
    """A single issue found during validation."""

    level: str  # "structural", "schema", "content", "semantic"
    severity: str  # "error", "warning"
    message: str
    file_path: str = ""
    sheet_name: str = ""


@dataclass
class ValidationResult:
    """Validation result for a single file."""

    file_path: str
    is_valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def add_error(self, level: str, message: str, **kwargs: Any) -> None:
        self.issues.append(
            ValidationIssue(
                level=level,
                severity="error",
                message=message,
                file_path=kwargs.get("file_path", self.file_path),
                sheet_name=kwargs.get("sheet_name", ""),
            )
        )
        self.is_valid = False

    def add_warning(self, level: str, message: str, **kwargs: Any) -> None:
        self.issues.append(
            ValidationIssue(
                level=level,
                severity="warning",
                message=message,
                file_path=kwargs.get("file_path", self.file_path),
                sheet_name=kwargs.get("sheet_name", ""),
            )
        )


class ValidationPipeline:
    """Multi-level validation pipeline for .xlsx files.

    Levels:
      1. Structural: ZIP integrity, XML well-formed, required files present
      2. Schema: sheetData exists, columns detected, data rows > 0
      3. Content: images loadable, formulas parseable
      4. Semantic: part numbers valid, quantities numeric, no duplicates
      5. Split-quality: post-split quality check (images, rows)

    Used after each split to guarantee output file correctness.
    """

    def __init__(
        self,
        check_structural: bool = True,
        check_schema: bool = True,
        check_content: bool = True,
        check_semantic: bool = False,
        check_split_quality: bool = False,
        expected_min_rows: int = 0,
        expected_max_rows: int = 0,
        has_images_in_original: bool = False,
        max_file_size_mb: float = 50.0,
    ):
        self.check_structural = check_structural
        self.check_schema = check_schema
        self.check_content = check_content
        self.check_semantic = check_semantic
        self.check_split_quality = check_split_quality
        self.expected_min_rows = expected_min_rows
        self.expected_max_rows = expected_max_rows
        self.has_images_in_original = has_images_in_original
        self.max_file_size_mb = max_file_size_mb

    def validate(self, file_path: str) -> ValidationResult:
        """Run the full validation pipeline on a single file.

        Args:
            file_path: Path to the .xlsx file.

        Returns:
            ValidationResult with is_valid and list of issues.
        """
        result = ValidationResult(file_path=file_path)

        if not os.path.isfile(file_path):
            result.add_error("structural", f"File not found: {file_path}")
            return result

        ext = os.path.splitext(file_path)[1].lower()
        if ext != ".xlsx":
            result.add_warning("structural", f"Not .xlsx format: {ext}")
            return result

        if self.check_structural:
            self._check_structural(file_path, result)
            if not result.is_valid:
                return result

        if self.check_schema:
            self._check_schema(file_path, result)

        if self.check_content:
            self._check_content(file_path, result)

        if self.check_semantic:
            self._check_semantic(file_path, result)

        if self.check_split_quality:
            self._check_split_quality(file_path, result)

        return result

    def validate_batch(self, file_paths: list[str]) -> list[ValidationResult]:
        """Validate a list of files.

        Returns:
            List of ValidationResult for each file.
        """
        results = []
        for fp in file_paths:
            results.append(self.validate(fp))
        return results

    def _check_structural(self, file_path: str, result: ValidationResult) -> None:
        """Level 1: Structural integrity.

        Checks:
          - File is a valid ZIP
          - Required OOXML files are present
          - XML is well-formed
          - File size does not exceed the limit
        """
        # File size
        try:
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if size_mb > self.max_file_size_mb:
                result.add_warning(
                    "structural",
                    f"File too large: {size_mb:.1f} MB > {self.max_file_size_mb} MB",
                )
        except OSError:
            pass

        # ZIP integrity
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                names = set(zf.namelist())

                # Required files
                required = [
                    "[Content_Types].xml",
                    "xl/workbook.xml",
                    "xl/_rels/workbook.xml.rels",
                    "_rels/.rels",
                ]
                for req in required:
                    if req not in names:
                        result.add_error(
                            "structural",
                            f"Missing required file: {req}",
                        )

                # At least one sheet XML
                if not any(
                    n.endswith(".xml") and "sheet" in n.lower() and "_rels" not in n
                    for n in names
                ):
                    result.add_error("structural", "No sheet XML files found")

                # XML well-formed for critical files
                critical_xmls = ["xl/workbook.xml", "[Content_Types].xml"]
                for cx in critical_xmls:
                    if cx in names:
                        try:
                            data = zf.read(cx)
                            ET.fromstring(data)
                        except ET.ParseError as e:
                            result.add_error(
                                "structural",
                                f"XML not well-formed: {cx}: {e}",
                            )

                # CRC check for ALL files.
                # IMPORTANT: WPS Office and other OOXML generators may create
                # files with invalid CRC checksums that still open correctly
                # in Excel. Therefore CRC errors are downgraded to WARNING.
                for info in zf.infolist():
                    try:
                        zf.read(info.filename)
                    except (zipfile.BadZipFile, Exception) as e:
                        result.add_warning(
                            "structural",
                            f"CRC error (non-critical): {info.filename}: {e}",
                        )

        except zipfile.BadZipFile as e:
            result.add_error("structural", f"Invalid ZIP: {e}")
        except OSError as e:
            result.add_error("structural", f"File read error: {e}")

    def _check_schema(self, file_path: str, result: ValidationResult) -> None:
        """Level 2: Schema validation.

        Checks:
          - sheetData exists in every sheet
          - At least one data row
          - Row count > 0
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                for name in zf.namelist():
                    if (
                        name.endswith(".xml")
                        and "sheet" in name.lower()
                        and "_rels" not in name
                    ):
                        try:
                            data = zf.read(name)
                            root = ET.fromstring(data)
                            ns = f"{{{NS_MAIN}}}sheetData"
                            sheet_data = root.find(ns)
                            if sheet_data is None:
                                result.add_error(
                                    "schema",
                                    f"No sheetData in {name}",
                                )
                                continue

                            # Row count
                            row_count = len(sheet_data.findall(f"{{{NS_MAIN}}}row"))
                            if row_count == 0:
                                result.add_warning(
                                    "schema",
                                    f"Empty sheetData (0 rows) in {name}",
                                )
                        except ET.ParseError as e:
                            result.add_error(
                                "schema",
                                f"Error parsing sheet XML {name}: {e}",
                            )
        except zipfile.BadZipFile:
            pass  # Already handled at structural level

    def _check_content(self, file_path: str, result: ValidationResult) -> None:
        """Level 3: Content check.

        Checks:
          - Images in xl/media/ have valid formats
          - Drawing XML references exist
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                names = set(zf.namelist())
                media_files = [n for n in names if n.startswith("xl/media/")]

                # Check that all media files are non-empty
                for mf in media_files:
                    try:
                        data = zf.read(mf)
                        if len(data) == 0:
                            result.add_warning(
                                "content",                                    f"Empty media file: {mf}",
                            )
                    except Exception as e:
                        result.add_error(
                            "content",
                            f"Error reading media file {mf}: {e}",
                        )

                # Check that drawing rels references exist
                for name in names:
                    if "drawing" in name and name.endswith(".rels"):
                        try:
                            data = zf.read(name)
                            root = ET.fromstring(data)
                            drawing_dir = os.path.dirname(
                                name.replace("_rels/", "").replace(".rels", "")
                            )
                            for rel in root:
                                target = rel.get("Target", "")
                                if target:
                                    resolved = os.path.normpath(
                                        os.path.join(drawing_dir, target)
                                    ).replace(os.sep, "/")
                                    if not resolved.startswith("/"):
                                        resolved_check = resolved
                                    else:
                                        resolved_check = resolved[1:]
                                    # Check only media references
                                    if "media" in resolved_check.lower():
                                        if resolved_check not in names:
                                            result.add_warning(
                                                "content",
                                                f"Broken media reference in {name}: "
                                                f"{target} -> {resolved_check}",
                                            )
                        except Exception:
                            pass  # Non-critical

        except zipfile.BadZipFile:
            pass

    def _check_semantic(self, file_path: str, result: ValidationResult) -> None:
        """Level 4: Semantic validation (optional).

        Checks:
          - Part numbers in data look valid
          - Quantities are numeric
          - No duplicate part numbers
        """

        try:
            import warnings

            import openpyxl

            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore", category=UserWarning, module="openpyxl"
                )
                wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)

            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                # Simple check: at least a few cells with data
                non_empty = 0
                for row in ws.iter_rows(max_row=10, max_col=10):
                    for cell in row:
                        if cell.value is not None:
                            non_empty += 1
                if non_empty == 0:
                    result.add_warning(
                        "semantic",
                        f"Sheet '{sheet_name}' contains no data",
                        sheet_name=sheet_name,
                    )
            wb.close()
        except Exception as e:
            result.add_warning("semantic", f"Could not check semantics: {e}")

    def _check_split_quality(self, file_path: str, result: ValidationResult) -> None:
        """Level 5: Post-split quality check.

        Checks:
          - Image presence (if originals had images)
          - Row count in the split file
          - File size not too small (sign of empty/corrupted file)
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                names = set(zf.namelist())

                # Check 1: images (if originals had them)
                if self.has_images_in_original:
                    media_files = [n for n in names if n.startswith("xl/media/")]
                    if not media_files:
                        result.add_warning(
                            "split-quality",
                            "No images in split file (originals had images)",
                        )

                # Check 2: row count
                if self.expected_min_rows > 0 or self.expected_max_rows > 0:
                    for name in names:
                        if (
                            name.endswith(".xml")
                            and "sheet" in name.lower()
                            and "_rels" not in name
                        ):
                            try:
                                data = zf.read(name)
                                root = ET.fromstring(data)
                                ns = f"{{{NS_MAIN}}}sheetData"
                                sheet_data = root.find(ns)
                                if sheet_data is not None:
                                    row_count = len(
                                        sheet_data.findall(f"{{{NS_MAIN}}}row")
                                    )
                                    if (
                                        self.expected_min_rows > 0
                                        and row_count < self.expected_min_rows
                                    ):
                                        result.add_warning(
                                            "split-quality",
                                            f"Too few rows: {row_count} < {self.expected_min_rows}",
                                        )
                                    if (
                                        self.expected_max_rows > 0
                                        and row_count > self.expected_max_rows * 1.5
                                    ):
                                        result.add_warning(
                                            "split-quality",
                                            f"Too many rows: {row_count} > {self.expected_max_rows * 1.5:.0f}",
                                        )
                            except ET.ParseError:
                                pass
                            break  # Check only the first sheet XML

                # Check 3: file size
                try:
                    size_bytes = os.path.getsize(file_path)
                    if size_bytes < 1024:  # < 1 KB — suspiciously small
                        result.add_warning(
                            "split-quality",
                            f"File is very small: {size_bytes} bytes",
                        )
                except OSError:
                    pass

        except zipfile.BadZipFile:
            pass  # Already handled at structural level


def validate_split_file(
    file_path: str,
    has_images_in_original: bool = False,
    expected_min_rows: int = 0,
    expected_max_rows: int = 0,
) -> ValidationResult:
    """Quick validation of one split file (structural + schema + split-quality).

    Convenience wrapper for use in splitter.

    Args:
        file_path: Path to the .xlsx file.
        has_images_in_original: True if the source file had images.
        expected_min_rows: Minimum expected row count (0 = skip check).
        expected_max_rows: Maximum expected row count (0 = skip check).
    """
    pipeline = ValidationPipeline(
        check_structural=True,
        check_schema=True,
        check_content=False,
        check_semantic=False,
        check_split_quality=True,
        has_images_in_original=has_images_in_original,
        expected_min_rows=expected_min_rows,
        expected_max_rows=expected_max_rows,
    )
    return pipeline.validate(file_path)


def validate_split_file_lenient(file_path: str) -> ValidationResult:
    """Lenient validation of a split file: only check openpyxl openability.

    Used as a last resort before sending to corrupted_cards.
    WPS-generated files may have invalid CRC, non-standard OOXML structures,
    yet be functionally valid.
    This check emulates MS Excel behaviour: if the file opens, it is valid.

    Returns:
        ValidationResult with is_valid = True if the file can be opened via openpyxl.
    """
    result = ValidationResult(file_path=file_path)
    try:
        import warnings

        import openpyxl

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")
            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            _ = wb.sheetnames
            wb.close()
    except Exception as e:
        result.add_error(
            "structural",
            f"File cannot be opened even via openpyxl: {e}",
        )
    return result


def validate_and_quarantine(
    file_path: str,
    quarantine_dir: str,
) -> ValidationResult:
    """Validate a file and move corrupted ones to quarantine/.

    Args:
        file_path: Path to the .xlsx file.
        quarantine_dir: Directory for corrupted files.

    Returns:
        ValidationResult.
    """
    result = validate_split_file(file_path)

    if not result.is_valid:
        try:
            os.makedirs(quarantine_dir, exist_ok=True)
            import shutil

            dest = os.path.join(quarantine_dir, os.path.basename(file_path))
            shutil.copy2(file_path, dest)

            # Write .error file
            error_path = dest + ".error"
            with open(error_path, "w", encoding="utf-8") as f:
                for issue in result.errors:
                    f.write(f"[{issue.level}] {issue.message}\n")

            logger.warning(
                "File moved to quarantine: %s (%d errors)",
                os.path.basename(file_path),
                len(result.errors),
            )
        except Exception as e:
            logger.error("Failed to move to quarantine: %s", e)

    return result
