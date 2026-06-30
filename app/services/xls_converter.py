""".xls → .xlsx conversion module via LibreOffice.

Uses LibreOffice headless mode to convert legacy .xls files
to modern .xlsx format while preserving images, formatting,
and data structure.

Advantages over xlrd:
  - Preserves images (embedded OLE objects, drawings)
  - Preserves conditional formatting
  - Preserves merged cells correctly
  - Supports legacy .xls (BIFF) formats

Note:
  - Requires LibreOffice installed on the system
  - Conversion runs via subprocess (headless mode)
  - Temp files are created in the specified directory
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Cached path to LibreOffice binary
_libreoffice_path: str | None = None


def find_libreoffice() -> str | None:
    """Find the path to the LibreOffice binary.

    Checks:
      1. LIBREOFFICE_PATH environment variable
      2. which libreoffice / which soffice
      3. Typical installation paths

    Returns:
        Path to LibreOffice or None if not found.
    """
    global _libreoffice_path
    if _libreoffice_path is not None:
        return _libreoffice_path

    # 1. Environment variable
    env_path = os.environ.get("LIBREOFFICE_PATH")
    if env_path and os.path.isfile(env_path):
        _libreoffice_path = env_path
        return _libreoffice_path

    # 2. which libreoffice / soffice
    for cmd in ("libreoffice", "soffice"):
        try:
            result = subprocess.run(
                ["which", cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                path = result.stdout.strip()
                if os.path.isfile(path):
                    _libreoffice_path = path
                    return _libreoffice_path
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    # 3. Typical paths
    typical_paths = [
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
        "/usr/local/bin/libreoffice",
        "/usr/local/bin/soffice",
        "/snap/bin/libreoffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "C:\\Program Files\\LibreOffice\\program\\soffice.exe",
        "C:\\Program Files (x86)\\LibreOffice\\program\\soffice.exe",
    ]
    for path in typical_paths:
        if os.path.isfile(path):
            _libreoffice_path = path
            return _libreoffice_path

    return None


def is_libreoffice_available() -> bool:
    """Check whether LibreOffice is available."""
    return find_libreoffice() is not None


def convert_xls_to_xlsx(
    xls_path: str,
    output_dir: str | None = None,
    timeout: int = 120,
) -> str | None:
    """Convert a .xls file to .xlsx via LibreOffice.

    Args:
        xls_path: Path to the source .xls file.
        output_dir: Directory to save the result.
                    If None, a temp directory is created.
        timeout: Conversion timeout in seconds.

    Returns:
        Path to the converted .xlsx file, or None on error.
    """
    lo_path = find_libreoffice()
    if lo_path is None:
        logger.warning(
            "LibreOffice not found. .xls → .xlsx conversion impossible. "
            "Install LibreOffice or set the LIBREOFFICE_PATH "
            "environment variable."
        )
        return None

    if not os.path.isfile(xls_path):
        logger.error("File not found: %s", xls_path)
        return None

    ext = os.path.splitext(xls_path)[1].lower()
    if ext != ".xls":
        logger.debug("Not a .xls file, conversion not needed: %s", xls_path)
        return None

    # Create a temp output directory if not specified
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="burlak_xls_convert_")
    else:
        os.makedirs(output_dir, exist_ok=True)

    try:
        logger.info(
            "Converting .xls → .xlsx: %s → %s",
            os.path.basename(xls_path),
            output_dir,
        )

        # LibreOffice headless conversion
        cmd = [
            lo_path,
            "--headless",
            "--convert-to",
            "xlsx",
            "--outdir",
            output_dir,
            xls_path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            logger.warning(
                "LibreOffice conversion exited with code %d: %s",
                result.returncode,
                result.stderr[:500] if result.stderr else "",
            )

        # Find the converted file
        base_name = os.path.splitext(os.path.basename(xls_path))[0]
        xlsx_path = os.path.join(output_dir, f"{base_name}.xlsx")

        if os.path.isfile(xlsx_path):
            file_size = os.path.getsize(xlsx_path)
            logger.info(
                "Conversion succeeded: %s (%.1f MB)",
                os.path.basename(xlsx_path),
                file_size / (1024 * 1024),
            )
            return xlsx_path

        # Fallback: search for any .xlsx file in output_dir with a similar name
        for fn in os.listdir(output_dir):
            if fn.endswith(".xlsx") and base_name[:10] in fn:
                found_path = os.path.join(output_dir, fn)
                logger.info("Found converted file: %s", fn)
                return found_path

        logger.warning(
            "Converted .xlsx file not found in %s. LibreOffice output: %s",
            output_dir,
            result.stdout[:500] if result.stdout else "",
        )
        return None

    except subprocess.TimeoutExpired:
        logger.error(
            "Conversion timeout (%d sec): %s",
            timeout,
            os.path.basename(xls_path),
        )
        return None
    except FileNotFoundError:
        logger.error("LibreOffice not found: %s", lo_path)
        return None
    except Exception as e:
        logger.error("Error converting .xls → .xlsx: %s", e)
        return None


def convert_xls_files_batch(
    xls_files: list[str],
    temp_dir: str,
    max_workers: int = 2,
) -> dict[str, str]:
    """Convert a batch of .xls files to .xlsx.

    Args:
        xls_files: List of paths to .xls files.
        temp_dir: Directory for converted files.
        max_workers: Maximum number of parallel conversions.

    Returns:
        Dict {original_path: converted_xlsx_path}.
        Files that could not be converted are absent from the dict.
    """
    if not xls_files:
        return {}

    if not is_libreoffice_available():
        logger.warning(
            "LibreOffice not available. %d .xls files will not be converted.",
            len(xls_files),
        )
        return {}

    os.makedirs(temp_dir, exist_ok=True)
    converted: dict[str, str] = {}

    logger.info(
        "Converting %d .xls files via LibreOffice...",
        len(xls_files),
    )

    for xls_path in xls_files:
        xlsx_path = convert_xls_to_xlsx(xls_path, output_dir=temp_dir)
        if xlsx_path:
            converted[xls_path] = xlsx_path
            logger.info(
                "  ✓ %s → %s",
                os.path.basename(xls_path),
                os.path.basename(xlsx_path),
            )
        else:
            logger.warning(
                "  ✗ Conversion failed: %s",
                os.path.basename(xls_path),
            )

    logger.info(
        "Conversion complete: %d/%d succeeded",
        len(converted),
        len(xls_files),
    )
    return converted
