"""Safe fuzzy matching module for catalog part numbers.

Key rule:
  - A match is ONLY considered for differences in dashes, spaces and special chars.
  - A difference in even ONE digit (e.g. "ABCD123" vs "ABCD124") is NOT a match.
    These are completely different parts in manufacturing.

Algorithm:
  1. Normalize both numbers: remove all spaces, dashes, special chars.
  2. Compare normalized strings.
  3. If identical — it's a fuzzy match.
  4. If different — NOT a match, even by one character.

Additionally:
  - Part number integrity check: filtering out garbage that doesn't look like
    a catalog number.
"""

from __future__ import annotations

import logging

from app.services.normalizer import (
    is_valid_part_number as _is_valid_part_number_strict,
)
from app.services.normalizer import (
    normalize_part_number,
)

logger = logging.getLogger(__name__)


def is_fuzzy_match(part_a: str, part_b: str) -> bool:
    """Check whether two part numbers are a fuzzy match.

    Considers a match only for numbers that are identical after cleaning
    special characters. Differences in digits/letters are NOT allowed.

    Args:
        part_a: First part number.
        part_b: Second part number.

    Returns:
        True if the numbers match after normalization.

    Example:
        is_fuzzy_match("ABCD-123", "ABCD123") -> True
        is_fuzzy_match("ABCD 123", "ABCD-123") -> True
        is_fuzzy_match("ABCD123", "ABCD124") -> False  # digit difference!
        is_fuzzy_match("ABCD-123", "ABCD-123") -> True  # exact match
    """
    return normalize_part_number(part_a) == normalize_part_number(part_b)


def is_valid_part_number(part_no: str) -> bool:
    """Check whether a string looks like a catalog part number.

    Uses LENIENT check — allows letters-only or digits-only strings
    of sufficient length (for fuzzy matching).

    For strict checking, use normalizer.is_valid_part_number().

    Args:
        part_no: String to check.

    Returns:
        True if the string looks like a catalog part number.
    """
    return _is_valid_part_number_strict(part_no, strict=False)


class FuzzyMatcher:
    """Fuzzy part number matching service.

    Builds an index of normalized numbers for fast lookup.
    """

    def __init__(self, bom_part_numbers: set[str]):
        """Initialize the matcher.

        Args:
            bom_part_numbers: Set of part numbers from BOM.
        """
        # Index: normalized -> list of original numbers
        self._normalized_index: dict[str, list[str]] = {}

        for pn in bom_part_numbers:
            norm = normalize_part_number(pn)
            if norm not in self._normalized_index:
                self._normalized_index[norm] = []
            self._normalized_index[norm].append(pn)

    def find_fuzzy_match(self, cards_part_no: str) -> str | None:
        """Find a fuzzy match for a card part number in the BOM index.

        Args:
            cards_part_no: Part number from an operational card.

        Returns:
            Original part number from BOM, or None if no match found.
        """
        norm = normalize_part_number(cards_part_no)
        matches = self._normalized_index.get(norm, [])
        if matches:
            # Return the first one (usually there's only one)
            return matches[0]
        return None

    def get_normalized(self, part_no: str) -> str:
        """Get the normalized form of a part number."""
        return normalize_part_number(part_no)
