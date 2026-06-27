"""Data normalization module for BOM and operational cards.

Universal cleanup and transformation of cell values:
  - Quantities: "–" → 0, "– –" → 0, empty → 0, numbers as-is
  - Part numbers: remove special chars, uppercase
  - Names: remove extra whitespace, line breaks

S/- markers are handled at BOM parser level:
  - BOM: "S" means "part present, quantity from qty column"
  - BOM: "–"/empty means "part absent, quantity = 0"
  - Cards: S/- markers do not appear

Used as the central normalization module to avoid duplicating
logic across BOM and Card parsers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Pattern for detecting a number (integer or decimal, comma or dot separator)
NUMERIC_RE = re.compile(r"^\s*[-+]?\d+(?:[\.,]\d+)?\s*$")

# Pattern for "S"-like markers (BAIC: "S" = part present)
S_MARKER_RE = re.compile(r"^\s*[sS]\s*$")

# Pattern for dash markers (BAIC: "–" = no part)
DASH_MARKER_RE = re.compile(r"^\s*[\-\–\—\‒\―]{1,3}\s*$")

# Pattern for "– –" (double dash)
DOUBLE_DASH_RE = re.compile(r"^\s*[\-\–\—\‒\―]\s*[\-\–\—\‒\―]\s*$")

# Characters to clean from part numbers (including various dash types)
CLEAN_PN_CHARS = re.compile(
    r"[\s\-\u2013\u2014\u2012\u2015\.\,\/\_\,\;\:\'\"\(\)\[\]\{\}\|\\]+"
)

# Chinese ideographs
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")

# Cyrillic characters
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")

# Pattern for lenient validation (fuzzy matcher): letters+digits >=3, or digits >=3
PART_NUMBER_LENIENT_RE = re.compile(
    r"^(?=.*[A-Za-z])[A-Za-z0-9\-\.\/\_\s]{3,}$"
    r"|^\d{3,}$"
)

# Excel XML encoding artifacts
XML_ARTIFACTS_RE = re.compile(r"(_x[0-9a-fA-F]{4}_|\r\n|[\r\n])", re.IGNORECASE)


class QuantityNormalizer:
    """Normalizer for quantity values.

    Supports:
      - Numbers (int, float, str)
      - Dash markers: "–" → 0 (no part)
      - Empty/None values → 0
      - String numbers: "2.5" → 2.5, "10" → 10.0

    S markers are NOT handled here — their processing is at BOM parser level
    (S means "part present, quantity from qty column").
    """

    @staticmethod
    def normalize(value: Any, default: float = 0.0) -> float:
        """Normalize a quantity value to float.

        Args:
            value: Raw cell value.
            default: Default value when conversion is impossible.

        Returns:
            Numeric quantity value.

        Examples:
            >>> QuantityNormalizer.normalize(42)
            42.0
            >>> QuantityNormalizer.normalize("S")
            0.0
            >>> QuantityNormalizer.normalize("–")
            0.0
            >>> QuantityNormalizer.normalize(None)
            0.0
            >>> QuantityNormalizer.normalize("2.5")
            2.5
        """
        if value is None:
            return default

        # Reject non-numeric types that could be misinterpreted
        # bool is subclass of int, so check before int
        if isinstance(value, bool):
            logger.debug("bool value detected: %s → 0.0", value)
            return default
        # datetime/timedelta from Excel date columns
        from datetime import date, datetime, timedelta

        if isinstance(value, (datetime, date, timedelta)):
            logger.debug("datetime value detected: %s → 0.0", value)
            return default

        # Numbers (int/float) — return as-is
        if isinstance(value, (int, float)):
            return float(value)

        # Strings
        s = str(value).strip()
        # Remove Excel XML encoding artifacts
        match = XML_ARTIFACTS_RE.search(s)
        if match:
            s = s[: match.start()]
        s = s.strip()
        if not s:
            return default

        # "–" / "—" / "– –" = no part → 0
        if DASH_MARKER_RE.match(s) or DOUBLE_DASH_RE.match(s):
            logger.debug("dash-marker detected: '%s' → 0.0", value)
            return 0.0

        # String numbers: "2.5", "10", "-3"
        if NUMERIC_RE.match(s):
            try:
                # Replace comma with dot (European format)
                normalized = s.replace(",", ".")
                return float(normalized)
            except (ValueError, TypeError):
                return default

        # Unrecognized format (including "S") — log and return default
        logger.debug(
            "Unrecognized quantity format: '%s' (type=%s) → default=%.1f",
            value,
            type(value).__name__,
            default,
        )
        return default


class PartNumberNormalizer:
    """Part number normalizer.

    Removes special characters, converts to uppercase.
    Guarantees consistency of part numbers between BOM and cards.
    """

    @staticmethod
    def normalize(part_no: str) -> str:
        """Normalize a part number for comparison.

        Args:
            part_no: Raw part number.

        Returns:
            Normalized part number (only letters and digits, upper case).

        Examples:
            >>> PartNumberNormalizer.normalize("5306200-ED001-AC00000")
            '5306200ED001AC00000'
            >>> PartNumberNormalizer.normalize("ab-123-cd")
            'AB123CD'
            >>> PartNumberNormalizer.normalize(" A.1/B_2 ")
            'A1B2'
        """
        if not part_no or not isinstance(part_no, str):
            return ""
        s = part_no.strip()
        # Remove Excel XML encoding artifacts (carriage return / newline)
        # Take ONLY FIRST part before the artifact (dual values: Chinese + English)
        match = XML_ARTIFACTS_RE.search(s)
        if match:
            s = s[: match.start()]
        cleaned = CLEAN_PN_CHARS.sub("", s)
        return cleaned.upper()

    @staticmethod
    def is_valid(part_no: str, strict: bool = True) -> bool:
        """Check whether a string looks like a part number.

        Args:
            part_no: String to check.
            strict: True = strict check (letters+digits, or 4+ digits).
                    False = lenient check (fuzzy matcher: letters+digits >=3, or digits >=3).

        Returns:
            True if the string looks like a part number.
        """
        if not part_no or not isinstance(part_no, str):
            return False
        cleaned = part_no.strip()
        if len(cleaned) < 3:
            return False
        # Garbage values
        garbage = {"n/a", "na", "none", "无", "null", "-", "--", "---", "/"}
        if cleaned.lower() in garbage:
            return False
        # Strings with Chinese ideographs or Cyrillic are not part numbers
        if _CJK_RE.search(cleaned) or _CYRILLIC_RE.search(cleaned):
            return False
        if strict:
            # Strict check: letters+digits, or minimum 4 digits
            has_alpha = bool(re.search(r"[A-Za-z]", cleaned))
            has_digit = bool(re.search(r"\d", cleaned))
            if has_alpha and has_digit:
                return True
            if has_digit and len(cleaned) >= 4:
                return True
            return False
        else:
            # Lenient check (fuzzy matcher): letters+digits >=3, or digits >=3
            return bool(PART_NUMBER_LENIENT_RE.match(cleaned))


class NameNormalizer:
    """Part name normalizer.

    Removes extra whitespace, line breaks, normalizes formatting.
    """

    @staticmethod
    def normalize(name: str) -> str:
        """Normalize a part name.

        Args:
            name: Raw name.

        Returns:
            Normalized name.

        Examples:
            >>> NameNormalizer.normalize("  Part  assembly  ")
            'Part assembly'
            >>> NameNormalizer.normalize("Line\\nHarness")
            'Line Harness'
        """
        if not name or not isinstance(name, str):
            return ""
        # Remove line breaks and carriage returns
        text = name.replace("\n", " ").replace("\r", "").replace("\t", " ")
        # Collapse multiple spaces
        text = " ".join(text.split())
        return text.strip()


def normalize_quantity(value: Any, default: float = 0.0) -> float:
    """Convenience wrapper for quantity normalization.

    Used in BOM and Card parsers for uniform processing.
    """
    return QuantityNormalizer.normalize(value, default=default)


def normalize_part_number(part_no: str) -> str:
    """Convenience wrapper for part number normalization."""
    return PartNumberNormalizer.normalize(part_no)


def clean_part_number(part_no: str) -> str:
    """Clean part number from special characters, convert to uppercase.

    Alias for normalize_part_number() — single source of truth for all modules.
    """
    return PartNumberNormalizer.normalize(part_no)


def is_valid_part_number(part_no: str, strict: bool = True) -> bool:
    """Check whether a string looks like a part number.

    Alias for PartNumberNormalizer.is_valid() — single source of truth.
    strict=True  — strict check (for BOM and Card parsers)
    strict=False — lenient check (for fuzzy matcher)
    """
    return PartNumberNormalizer.is_valid(part_no, strict=strict)
