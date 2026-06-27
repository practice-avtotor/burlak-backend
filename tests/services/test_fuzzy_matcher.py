"""Unit tests for app/services/fuzzy_matcher.py.

Covers:
  - is_fuzzy_match: exact, fuzzy, different numbers, empty strings
  - is_valid_part_number: lenient validation
  - FuzzyMatcher: index, find_fuzzy_match, get_normalized, edge cases
"""

from __future__ import annotations

from app.services.fuzzy_matcher import (
    FuzzyMatcher,
    is_fuzzy_match,
    is_valid_part_number,
)

# ═══════════════════════════════════════════════════════════════════════
#  is_fuzzy_match
# ═══════════════════════════════════════════════════════════════════════


class TestIsFuzzyMatch:
    def test_exact_match(self):
        assert is_fuzzy_match("5306200ED001", "5306200ED001") is True

    def test_dash_vs_no_dash(self):
        assert is_fuzzy_match("5306200-ED001", "5306200ED001") is True

    def test_space_vs_dash(self):
        assert is_fuzzy_match("ABC 123", "ABC-123") is True

    def test_different_case(self):
        assert is_fuzzy_match("abc-123", "ABC-123") is True

    def test_different_digit_fails(self):
        assert is_fuzzy_match("ABCD123", "ABCD124") is False

    def test_different_length_fails(self):
        assert is_fuzzy_match("ABCD-123", "ABCD-1234") is False

    def test_extra_letter_fails(self):
        assert is_fuzzy_match("ABCD-123", "ABCE-123") is False

    def test_em_dash_vs_regular_dash(self):
        assert is_fuzzy_match("ABC—123", "ABC-123") is True

    def test_dot_vs_dash(self):
        assert is_fuzzy_match("ABC.123", "ABC-123") is True

    def test_empty_strings(self):
        assert is_fuzzy_match("", "") is True

    def test_one_empty_string(self):
        assert is_fuzzy_match("", "ABC") is False


# ═══════════════════════════════════════════════════════════════════════
#  is_valid_part_number (lenient — from fuzzy_matcher)
# ═══════════════════════════════════════════════════════════════════════


class TestIsValidPartNumberLenient:
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

    def test_letters_only_valid_in_lenient(self):
        """Letters-only is valid in lenient mode (>=3 chars + letter)."""
        assert is_valid_part_number("ABCDEF") is True

    def test_numeric_3_digits(self):
        assert is_valid_part_number("123") is True


# ═══════════════════════════════════════════════════════════════════════
#  FuzzyMatcher — basic
# ═══════════════════════════════════════════════════════════════════════


class TestFuzzyMatcherBasic:
    def test_single_part(self):
        fm = FuzzyMatcher({"5306200-ED001"})
        assert fm.find_fuzzy_match("5306200ED001") == "5306200-ED001"

    def test_exact_match(self):
        fm = FuzzyMatcher({"ABC123"})
        assert fm.find_fuzzy_match("ABC123") == "ABC123"

    def test_no_match(self):
        fm = FuzzyMatcher({"ABC123"})
        assert fm.find_fuzzy_match("XYZ999") is None

    def test_normalize_retrieval(self):
        fm = FuzzyMatcher({"ABC-123"})
        assert fm.get_normalized("ABC-123") == "ABC123"

    def test_multiple_parts(self):
        fm = FuzzyMatcher({"P001", "5306200-ED001", "ABC 123"})
        assert fm.find_fuzzy_match("5306200ED001") == "5306200-ED001"
        assert fm.find_fuzzy_match("ABC-123") == "ABC 123"
        assert fm.find_fuzzy_match("P001") == "P001"

    def test_empty_set(self):
        fm = FuzzyMatcher(set())
        assert fm.find_fuzzy_match("ABC123") is None


# ═══════════════════════════════════════════════════════════════════════
#  FuzzyMatcher — edge cases
# ═══════════════════════════════════════════════════════════════════════


class TestFuzzyMatcherEdgeCases:
    def test_duplicate_normalized_forms(self):
        fm = FuzzyMatcher({"5306200-ED001", "5306200ED001"})
        result = fm.find_fuzzy_match("5306200-ED001")
        assert result is not None
        assert result in ("5306200-ED001", "5306200ED001")

    def test_case_insensitive_match(self):
        fm = FuzzyMatcher({"ABC-123"})
        assert fm.find_fuzzy_match("abc-123") == "ABC-123"

    def test_spaces_in_bom_part(self):
        fm = FuzzyMatcher({"ABC 123"})
        assert fm.find_fuzzy_match("ABC-123") == "ABC 123"

    def test_dots_in_bom_part(self):
        fm = FuzzyMatcher({"ABC.123"})
        assert fm.find_fuzzy_match("ABC-123") == "ABC.123"

    def test_get_normalized_for_unknown(self):
        fm = FuzzyMatcher({"ABC123"})
        assert fm.get_normalized("DEF-456") == "DEF456"

    def test_long_part_numbers(self):
        long_pn = "A" * 50 + "-123"
        fm = FuzzyMatcher({long_pn})
        assert fm.find_fuzzy_match(long_pn.replace("-", "")) == long_pn

    def test_unicode_chars_in_part(self):
        fm = FuzzyMatcher({"ABC—123"})
        assert fm.find_fuzzy_match("ABC-123") == "ABC—123"


# ═══════════════════════════════════════════════════════════════════════
#  FuzzyMatcher — real-world scenarios
# ═══════════════════════════════════════════════════════════════════════


class TestFuzzyMatcherRealWorld:
    def test_t1l_part_number(self):
        fm = FuzzyMatcher({"5306200-ED001", "551002664AA", "Q146Z0825F36"})
        assert fm.find_fuzzy_match("5306200ED001") == "5306200-ED001"
        assert fm.find_fuzzy_match("551002664AA") == "551002664AA"
        assert fm.find_fuzzy_match("Q146Z0825F36") == "Q146Z0825F36"

    def test_swm_part_number(self):
        fm = FuzzyMatcher({"4007100-ED002-AA00000"})
        assert fm.find_fuzzy_match("4007100ED002AA00000") == "4007100-ED002-AA00000"
        assert fm.find_fuzzy_match("4007100-ED002-AA00000") == "4007100-ED002-AA00000"

    def test_no_false_match_on_similar_parts(self):
        fm = FuzzyMatcher({"ABCD123", "ABCD124"})
        assert fm.find_fuzzy_match("ABCD125") is None
        assert fm.find_fuzzy_match("ABCD124") == "ABCD124"
        assert fm.find_fuzzy_match("ABCD123") == "ABCD123"
