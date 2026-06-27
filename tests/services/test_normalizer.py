"""Unit tests for app/services/normalizer.py.

Covers:
  - QuantityNormalizer: numbers, dashes, empties, strings, bools, datetime
  - PartNumberNormalizer: cleaning, uppercase, validation (strict/lenient)
  - NameNormalizer: whitespace, line breaks, multiple spaces
  - Convenience wrappers: normalize_quantity, normalize_part_number, etc.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.normalizer import (
    NameNormalizer,
    PartNumberNormalizer,
    QuantityNormalizer,
    clean_part_number,
    is_valid_part_number,
    normalize_part_number,
    normalize_quantity,
)

# ═══════════════════════════════════════════════════════════════════════
#  QuantityNormalizer
# ═══════════════════════════════════════════════════════════════════════


class TestQuantityNormalizer:
    def test_int_value(self):
        assert QuantityNormalizer.normalize(42) == 42.0

    def test_float_value(self):
        assert QuantityNormalizer.normalize(3.14) == 3.14

    def test_zero(self):
        assert QuantityNormalizer.normalize(0) == 0.0

    def test_negative(self):
        assert QuantityNormalizer.normalize(-5) == -5.0

    def test_none_returns_default(self):
        assert QuantityNormalizer.normalize(None) == 0.0

    def test_none_returns_custom_default(self):
        assert QuantityNormalizer.normalize(None, default=1.0) == 1.0

    def test_bool_returns_default(self):
        assert QuantityNormalizer.normalize(True) == 0.0
        assert QuantityNormalizer.normalize(False) == 0.0

    def test_datetime_returns_default(self):
        assert QuantityNormalizer.normalize(datetime.now()) == 0.0

    def test_date_returns_default(self):
        assert QuantityNormalizer.normalize(date.today()) == 0.0

    def test_timedelta_returns_default(self):
        assert QuantityNormalizer.normalize(timedelta(hours=1)) == 0.0

    def test_string_number(self):
        assert QuantityNormalizer.normalize("2.5") == 2.5

    def test_string_integer(self):
        assert QuantityNormalizer.normalize("10") == 10.0

    def test_string_negative(self):
        assert QuantityNormalizer.normalize("-3") == -3.0

    def test_string_with_comma(self):
        """European decimal format: comma as decimal separator."""
        assert QuantityNormalizer.normalize("3,14") == 3.14

    def test_dash_marker(self):
        assert QuantityNormalizer.normalize("–") == 0.0

    def test_em_dash_marker(self):
        assert QuantityNormalizer.normalize("—") == 0.0

    def test_double_dash_marker(self):
        assert QuantityNormalizer.normalize("– –") == 0.0

    def test_empty_string(self):
        assert QuantityNormalizer.normalize("") == 0.0

    def test_whitespace_only(self):
        assert QuantityNormalizer.normalize("   ") == 0.0

    def test_s_marker_returns_default(self):
        """S marker is not a number — returns default."""
        assert QuantityNormalizer.normalize("S") == 0.0
        assert QuantityNormalizer.normalize("s") == 0.0

    def test_non_numeric_string(self):
        assert QuantityNormalizer.normalize("abc") == 0.0


# ═══════════════════════════════════════════════════════════════════════
#  PartNumberNormalizer
# ═══════════════════════════════════════════════════════════════════════


class TestPartNumberNormalizer:
    def test_removes_dashes(self):
        assert PartNumberNormalizer.normalize("5306200-ED001") == "5306200ED001"

    def test_removes_spaces(self):
        assert PartNumberNormalizer.normalize("ABC 123") == "ABC123"

    def test_removes_dots(self):
        assert PartNumberNormalizer.normalize("ABC.1.2.3") == "ABC123"

    def test_removes_em_dash(self):
        assert PartNumberNormalizer.normalize("ABC—123") == "ABC123"

    def test_removes_slashes(self):
        assert PartNumberNormalizer.normalize("ABC/123/DEF") == "ABC123DEF"

    def test_removes_underscores(self):
        assert PartNumberNormalizer.normalize("ABC_123_DEF") == "ABC123DEF"

    def test_uppercases(self):
        assert PartNumberNormalizer.normalize("abc-123") == "ABC123"

    def test_multiple_dashes(self):
        assert PartNumberNormalizer.normalize("A-B-C---123") == "ABC123"

    def test_already_normalized(self):
        assert PartNumberNormalizer.normalize("ABC123DEF") == "ABC123DEF"

    def test_with_parentheses(self):
        assert PartNumberNormalizer.normalize("ABC(123)DEF") == "ABC123DEF"

    def test_mixed_special_chars(self):
        assert PartNumberNormalizer.normalize("A-B C.D/E_F") == "ABCDEF"

    def test_leading_trailing_whitespace(self):
        assert PartNumberNormalizer.normalize("  ABC-123  ") == "ABC123"

    def test_empty_string(self):
        assert PartNumberNormalizer.normalize("") == ""

    def test_none_returns_empty(self):
        assert PartNumberNormalizer.normalize(None) == ""  # type: ignore[arg-type]

    def test_only_special_chars(self):
        assert PartNumberNormalizer.normalize("--..  ..--") == ""


class TestPartNumberValidation:
    def test_letters_and_digits(self):
        assert PartNumberNormalizer.is_valid("ABC123DEF") is True

    def test_with_dashes(self):
        assert PartNumberNormalizer.is_valid("5306200-ED001") is True

    def test_numeric_only(self):
        assert PartNumberNormalizer.is_valid("12345") is True

    def test_short_string_invalid(self):
        assert PartNumberNormalizer.is_valid("AB") is False

    def test_empty_string_invalid(self):
        assert PartNumberNormalizer.is_valid("") is False

    def test_none_invalid(self):
        assert PartNumberNormalizer.is_valid(None) is False  # type: ignore[arg-type]

    def test_na_garbage(self):
        assert PartNumberNormalizer.is_valid("N/A") is False

    def test_none_word_garbage(self):
        assert PartNumberNormalizer.is_valid("None") is False

    def test_dash_garbage(self):
        assert PartNumberNormalizer.is_valid("—") is False

    def test_chinese_garbage(self):
        assert PartNumberNormalizer.is_valid("无") is False

    def test_3_digits_valid_strict(self):
        """3-digit number is NOT valid in strict mode (needs 4+ digits or alpha+digits)."""
        assert PartNumberNormalizer.is_valid("123", strict=True) is False

    def test_3_digits_valid_lenient(self):
        """3-digit number IS valid in lenient mode."""
        assert PartNumberNormalizer.is_valid("123", strict=False) is True

    def test_letters_only_strict(self):
        """Letters-only is NOT valid in strict mode (needs digits)."""
        assert PartNumberNormalizer.is_valid("ABCDEF", strict=True) is False

    def test_letters_only_lenient(self):
        """Letters-only IS valid in lenient mode (>= 3 chars + letter)."""
        assert PartNumberNormalizer.is_valid("ABCDEF", strict=False) is True

    def test_mixed_case(self):
        assert PartNumberNormalizer.is_valid("Abc-123-Def") is True


# ═══════════════════════════════════════════════════════════════════════
#  NameNormalizer
# ═══════════════════════════════════════════════════════════════════════


class TestNameNormalizer:
    def test_extra_whitespace(self):
        assert NameNormalizer.normalize("  Part  assembly  ") == "Part assembly"

    def test_line_breaks(self):
        assert NameNormalizer.normalize("Line\nHarness") == "Line Harness"

    def test_carriage_return(self):
        """\r is replaced with empty string (no space), then spaces are collapsed."""
        assert NameNormalizer.normalize("Line\rHarness") == "LineHarness"

    def test_tabs(self):
        assert NameNormalizer.normalize("A\tB\tC") == "A B C"

    def test_multiple_spaces(self):
        assert NameNormalizer.normalize("A    B    C") == "A B C"

    def test_empty_string(self):
        assert NameNormalizer.normalize("") == ""

    def test_none_returns_empty(self):
        assert NameNormalizer.normalize(None) == ""  # type: ignore[arg-type]

    def test_non_string_returns_empty(self):
        assert NameNormalizer.normalize(123) == ""  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
#  Convenience wrappers
# ═══════════════════════════════════════════════════════════════════════


class TestConvenienceWrappers:
    def test_normalize_quantity(self):
        assert normalize_quantity("2.5") == 2.5

    def test_normalize_part_number(self):
        assert normalize_part_number("ab-123") == "AB123"

    def test_clean_part_number(self):
        assert clean_part_number("A-B-C") == "ABC"

    def test_is_valid_part_number_strict(self):
        assert is_valid_part_number("ABC123") is True

    def test_is_valid_part_number_lenient(self):
        assert is_valid_part_number("ABCDEF", strict=False) is True
