"""Unit tests for app/services/normalizer.py.

Covers:
  - normalize_quantity: numbers, dashes, empties, strings, bools, datetime
  - normalize_part_number: cleaning, uppercase
  - is_valid_part_number: validation (strict/lenient)
  - normalize_name: whitespace, line breaks, multiple spaces
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from app.services.normalizer import (
    clean_part_number,
    is_valid_part_number,
    normalize_name,
    normalize_part_number,
    normalize_quantity,
)

# ═══════════════════════════════════════════════════════════════════════
#  normalize_quantity
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeQuantity:
    def test_int_value(self):
        assert normalize_quantity(42) == 42.0

    def test_float_value(self):
        assert normalize_quantity(3.14) == 3.14

    def test_zero(self):
        assert normalize_quantity(0) == 0.0

    def test_negative(self):
        assert normalize_quantity(-5) == -5.0

    def test_none_returns_default(self):
        assert normalize_quantity(None) == 0.0

    def test_none_returns_custom_default(self):
        assert normalize_quantity(None, default=1.0) == 1.0

    def test_bool_returns_default(self):
        assert normalize_quantity(True) == 0.0
        assert normalize_quantity(False) == 0.0

    def test_datetime_returns_default(self):
        assert normalize_quantity(datetime.now()) == 0.0

    def test_date_returns_default(self):
        assert normalize_quantity(date.today()) == 0.0

    def test_timedelta_returns_default(self):
        assert normalize_quantity(timedelta(hours=1)) == 0.0

    def test_string_number(self):
        assert normalize_quantity("2.5") == 2.5

    def test_string_integer(self):
        assert normalize_quantity("10") == 10.0

    def test_string_negative(self):
        assert normalize_quantity("-3") == -3.0

    def test_string_with_comma(self):
        """European decimal format: comma as decimal separator."""
        assert normalize_quantity("3,14") == 3.14

    def test_dash_marker(self):
        assert normalize_quantity("–") == 0.0

    def test_em_dash_marker(self):
        assert normalize_quantity("—") == 0.0

    def test_double_dash_marker(self):
        assert normalize_quantity("– –") == 0.0

    def test_empty_string(self):
        assert normalize_quantity("") == 0.0

    def test_whitespace_only(self):
        assert normalize_quantity("   ") == 0.0

    def test_s_marker_returns_default(self):
        """S marker is not a number — returns default."""
        assert normalize_quantity("S") == 0.0
        assert normalize_quantity("s") == 0.0

    def test_non_numeric_string(self):
        assert normalize_quantity("abc") == 0.0


# ═══════════════════════════════════════════════════════════════════════
#  normalize_part_number
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizePartNumber:
    def test_removes_dashes(self):
        assert normalize_part_number("5306200-ED001") == "5306200ED001"

    def test_removes_spaces(self):
        assert normalize_part_number("ABC 123") == "ABC123"

    def test_removes_dots(self):
        assert normalize_part_number("ABC.1.2.3") == "ABC123"

    def test_removes_em_dash(self):
        assert normalize_part_number("ABC—123") == "ABC123"

    def test_removes_slashes(self):
        assert normalize_part_number("ABC/123/DEF") == "ABC123DEF"

    def test_removes_underscores(self):
        assert normalize_part_number("ABC_123_DEF") == "ABC123DEF"

    def test_uppercases(self):
        assert normalize_part_number("abc-123") == "ABC123"

    def test_multiple_dashes(self):
        assert normalize_part_number("A-B-C---123") == "ABC123"

    def test_already_normalized(self):
        assert normalize_part_number("ABC123DEF") == "ABC123DEF"

    def test_with_parentheses(self):
        assert normalize_part_number("ABC(123)DEF") == "ABC123DEF"

    def test_mixed_special_chars(self):
        assert normalize_part_number("A-B C.D/E_F") == "ABCDEF"

    def test_leading_trailing_whitespace(self):
        assert normalize_part_number("  ABC-123  ") == "ABC123"

    def test_empty_string(self):
        assert normalize_part_number("") == ""

    def test_none_returns_empty(self):
        assert normalize_part_number(None) == ""  # type: ignore[arg-type]

    def test_only_special_chars(self):
        assert normalize_part_number("--..  ..--") == ""


class TestPartNumberValidation:
    def test_letters_and_digits(self):
        assert is_valid_part_number("ABC123DEF") is True

    def test_with_dashes(self):
        assert is_valid_part_number("5306200-ED001") is True

    def test_numeric_only(self):
        assert is_valid_part_number("12345") is True

    def test_short_string_invalid(self):
        assert is_valid_part_number("AB") is False

    def test_empty_string_invalid(self):
        assert is_valid_part_number("") is False

    def test_none_invalid(self):
        assert is_valid_part_number(None) is False  # type: ignore[arg-type]

    def test_na_garbage(self):
        assert is_valid_part_number("N/A") is False

    def test_none_word_garbage(self):
        assert is_valid_part_number("None") is False

    def test_dash_garbage(self):
        assert is_valid_part_number("—") is False

    def test_chinese_garbage(self):
        assert is_valid_part_number("无") is False

    def test_3_digits_valid_strict(self):
        """3-digit number is NOT valid in strict mode (needs 4+ digits or alpha+digits)."""
        assert is_valid_part_number("123", strict=True) is False

    def test_3_digits_valid_lenient(self):
        """3-digit number IS valid in lenient mode."""
        assert is_valid_part_number("123", strict=False) is True

    def test_letters_only_strict(self):
        """Letters-only is NOT valid in strict mode (needs digits)."""
        assert is_valid_part_number("ABCDEF", strict=True) is False

    def test_letters_only_lenient(self):
        """Letters-only IS valid in lenient mode (>= 3 chars + letter)."""
        assert is_valid_part_number("ABCDEF", strict=False) is True

    def test_mixed_case(self):
        assert is_valid_part_number("Abc-123-Def") is True


# ═══════════════════════════════════════════════════════════════════════
#  normalize_name
# ═══════════════════════════════════════════════════════════════════════


class TestNormalizeName:
    def test_extra_whitespace(self):
        assert normalize_name("  Part  assembly  ") == "Part assembly"

    def test_line_breaks(self):
        assert normalize_name("Line\nHarness") == "Line Harness"

    def test_carriage_return(self):
        """\r is replaced with empty string (no space), then spaces are collapsed."""
        assert normalize_name("Line\rHarness") == "LineHarness"

    def test_tabs(self):
        assert normalize_name("A\tB\tC") == "A B C"

    def test_multiple_spaces(self):
        assert normalize_name("A    B    C") == "A B C"

    def test_empty_string(self):
        assert normalize_name("") == ""

    def test_none_returns_empty(self):
        assert normalize_name(None) == ""  # type: ignore[arg-type]

    def test_non_string_returns_empty(self):
        assert normalize_name(123) == ""  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
#  Convenience wrappers / aliases
# ═══════════════════════════════════════════════════════════════════════


class TestConvenienceWrappers:
    def test_normalize_quantity_alias(self):
        assert normalize_quantity("2.5") == 2.5

    def test_normalize_part_number_alias(self):
        assert normalize_part_number("ab-123") == "AB123"

    def test_clean_part_number(self):
        assert clean_part_number("A-B-C") == "ABC"

    def test_is_valid_part_number_strict(self):
        assert is_valid_part_number("ABC123") is True

    def test_is_valid_part_number_lenient(self):
        assert is_valid_part_number("ABCDEF", strict=False) is True
