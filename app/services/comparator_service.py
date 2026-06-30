"""BOM-to-cards comparison (matching) module.

Performs simultaneous comparison of ALL configurations (not just one).
Uses safe fuzzy matching (hyphens, spaces, and special characters only).

Discrepancy types:
  - Only in BOM: part exists in BOM but not used in any card.
  - Only in cards: part exists in cards but missing from BOM for the config.
  - Quantity mismatch: card quantity differs from BOM quantity.
  - Fuzzy match: part found via approximate matching (different formatting).

MatchingEngine class — wrapper for FastAPI / server architecture use.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from app.schemas.cards import CardsData
from app.services.bom_parser_service import BOMData, PartInfo
from app.services.fuzzy_matcher import FuzzyMatcher
from app.services.normalizer import is_valid_part_number, normalize_part_number

logger = logging.getLogger(__name__)


class DiscrepancyType:
    """Discrepancy type constants."""

    ONLY_IN_BOM = "Есть в BOM, нет в операционных картах"
    ONLY_IN_CARDS = "Есть в операционных картах, нет в BOM"
    QUANTITY_MISMATCH = "Разное количество"
    FUZZY_MATCH = "Разный формат номера"


@dataclass
class Discrepancy:
    """A single discrepancy between BOM and operational cards."""

    part_number: str
    name_cn: str
    name_en: str
    qty_bom: float
    qty_cards: float
    card_numbers: list[str]
    discrepancy_type: str
    config_name: str = ""  # Which configuration this belongs to
    fuzzy_matched_to: str = ""  # Original BOM part number for fuzzy match
    name_ru: str = ""

    def __str__(self) -> str:
        config_info = f" [{self.config_name[:40]}]" if self.config_name else ""
        if self.discrepancy_type == DiscrepancyType.FUZZY_MATCH:
            return (
                f"[{self.discrepancy_type}] {self.part_number} -> {self.fuzzy_matched_to}: "
                f"BOM={self.qty_bom}, Карты={self.qty_cards}{config_info}"
            )
        return (
            f"[{self.discrepancy_type}] {self.part_number}: "
            f"BOM={self.qty_bom}, Карты={self.qty_cards}{config_info}"
        )


@dataclass
class ConfigComparisonResult:
    """Comparison result for a single configuration."""

    config_name: str
    discrepancies: list[Discrepancy]
    total_bom_parts: int = 0
    total_cards_parts: int = 0
    matched_parts: int = 0
    fuzzy_matched: int = 0  # Count of fuzzy matches


@dataclass
class MultiConfigComparisonResult:
    """Comparison result for ALL configurations."""

    config_results: list[ConfigComparisonResult]  # One per configuration
    all_discrepancies: list[Discrepancy]  # Combined list of all discrepancies
    total_configs: int = 0
    total_bom_unique_parts: int = 0  # Unique parts across all configs
    total_cards_unique_parts: int = 0  # Unique parts in cards


def compare_single_config(
    bom_parts: dict[str, PartInfo],
    cards_data: CardsData,
    config_name: str = "",
    fuzzy_matcher: FuzzyMatcher | None = None,
) -> ConfigComparisonResult:
    """Compare BOM and cards for a SINGLE configuration.

    Args:
        bom_parts: Configuration parts from BOM {part_no: PartInfo}.
        cards_data: Aggregated data from operational cards.
        config_name: Configuration name.
        fuzzy_matcher: Matcher for approximate comparison (None = exact only).

    Returns:
        ConfigComparisonResult with results.
    """
    discrepancies: list[Discrepancy] = []
    bom_part_numbers = set(bom_parts.keys())
    cards_part_numbers = set(cards_data.all_parts.keys())
    fuzzy_matched_count = 0

    # If a fuzzy matcher is provided, perform approximate comparison
    fuzzy_matched_pairs: dict[str, str] = {}  # cards_part -> bom_part
    if fuzzy_matcher:
        for cards_pn in list(cards_part_numbers):
            match = fuzzy_matcher.find_fuzzy_match(cards_pn)
            if match and match != cards_pn:
                # Fuzzy match found (and it's not an exact match)
                fuzzy_matched_pairs[cards_pn] = match
                fuzzy_matched_count += 1
                logger.debug(
                    "Fuzzy match: '%s' (cards) -> '%s' (BOM)",
                    cards_pn,
                    match,
                )

    # Build extended BOM set accounting for fuzzy matches
    extended_bom = set(bom_part_numbers)
    for cards_pn, bom_pn in fuzzy_matched_pairs.items():
        if bom_pn in bom_part_numbers:
            extended_bom.add(cards_pn)  # Add card variant as "known"

    # 1. Only in BOM — use original number format from BOM (with dashes, etc.)
    only_in_bom = bom_part_numbers - cards_part_numbers
    # Exclude those found via fuzzy match
    if fuzzy_matcher:
        cards_norm_set = {fuzzy_matcher.get_normalized(p) for p in cards_part_numbers}
        only_in_bom = {
            p
            for p in only_in_bom
            if fuzzy_matcher.get_normalized(p) not in cards_norm_set
        }

    for part_no in sorted(only_in_bom):
        part = bom_parts[part_no]
        if not is_valid_part_number(part_no):
            continue
        # Use original number format from BOM
        original_part_no = part.part_number if part.part_number else part_no
        discrepancies.append(
            Discrepancy(
                part_number=original_part_no,
                name_cn=part.name_cn,
                name_en=part.name_en,
                qty_bom=part.quantity,
                qty_cards=0.0,
                card_numbers=[],
                discrepancy_type=DiscrepancyType.ONLY_IN_BOM,
                config_name=config_name,
                name_ru=getattr(cards_data, "part_names_ru", {}).get(part_no, ""),
            )
        )

    # 2. Only in cards — show original card number if available
    only_in_cards = cards_part_numbers - bom_part_numbers
    # Exclude fuzzy matches
    only_in_cards = only_in_cards - set(fuzzy_matched_pairs.keys())

    for part_no in sorted(only_in_cards):
        if not is_valid_part_number(part_no):
            continue
        qty = cards_data.all_parts[part_no]
        card_numbers = _get_card_numbers(part_no, cards_data)
        # Use original number format from cards (if preserved)
        original_no = cards_data.original_part_numbers.get(part_no, part_no)
        discrepancies.append(
            Discrepancy(
                part_number=original_no,
                name_cn="",
                name_en="",
                qty_bom=0.0,
                qty_cards=qty,
                card_numbers=card_numbers,
                discrepancy_type=DiscrepancyType.ONLY_IN_CARDS,
                config_name=config_name,
                name_ru=getattr(cards_data, "part_names_ru", {}).get(part_no, ""),
            )
        )

    # 3. Fuzzy match — show original BOM number as primary
    for cards_pn, bom_pn in sorted(fuzzy_matched_pairs.items()):
        if not is_valid_part_number(cards_pn) or not is_valid_part_number(bom_pn):
            continue
        part = bom_parts.get(bom_pn)
        if part is None:
            continue
        cards_qty = cards_data.all_parts.get(cards_pn, 0.0)
        card_numbers = _get_card_numbers(cards_pn, cards_data)
        original_bom_no = part.part_number if part.part_number else bom_pn
        discrepancies.append(
            Discrepancy(
                part_number=original_bom_no,  # ← BOM original (with dashes)
                name_cn=part.name_cn,
                name_en=part.name_en,
                qty_bom=part.quantity,
                qty_cards=cards_qty,
                card_numbers=card_numbers,
                discrepancy_type=DiscrepancyType.FUZZY_MATCH,
                config_name=config_name,
                fuzzy_matched_to=cards_pn,  # ← card number (for reference)
                name_ru=getattr(cards_data, "part_names_ru", {}).get(cards_pn, ""),
            )
        )

    # 4. Quantity mismatch — use original number format from BOM
    common_parts = bom_part_numbers & cards_part_numbers
    matched = 0
    for part_no in sorted(common_parts):
        if not is_valid_part_number(part_no):
            continue
        bom_qty = bom_parts[part_no].quantity
        cards_qty = cards_data.all_parts.get(part_no, 0.0)

        if abs(bom_qty - cards_qty) > 0.001:
            part = bom_parts[part_no]
            card_numbers = _get_card_numbers(part_no, cards_data)
            original_part_no = part.part_number if part.part_number else part_no
            discrepancies.append(
                Discrepancy(
                    part_number=original_part_no,
                    name_cn=part.name_cn,
                    name_en=part.name_en,
                    qty_bom=bom_qty,
                    qty_cards=cards_qty,
                    card_numbers=card_numbers,
                    discrepancy_type=DiscrepancyType.QUANTITY_MISMATCH,
                    config_name=config_name,
                    name_ru=getattr(cards_data, "part_names_ru", {}).get(part_no, ""),
                )
            )
        else:
            matched += 1

    # Sort by type priority
    type_order = {
        DiscrepancyType.QUANTITY_MISMATCH: 0,
        DiscrepancyType.ONLY_IN_BOM: 1,
        DiscrepancyType.ONLY_IN_CARDS: 2,
        DiscrepancyType.FUZZY_MATCH: 3,
    }
    discrepancies.sort(
        key=lambda d: (type_order.get(d.discrepancy_type, 99), d.part_number)
    )

    return ConfigComparisonResult(
        config_name=config_name,
        discrepancies=discrepancies,
        total_bom_parts=len(bom_parts),
        total_cards_parts=len(cards_part_numbers),
        matched_parts=matched,
        fuzzy_matched=fuzzy_matched_count,
    )


def compare_single_config_cached(
    bom_parts: dict[str, PartInfo],
    cards_data: CardsData,
    config_name: str = "",
    cards_norm_set: set = None,
    fuzzy_matched_pairs: dict[str, str] = None,
    global_names: dict[str, tuple[str, str]] = None,
) -> ConfigComparisonResult:
    """Compare BOM and cards for a SINGLE configuration with pre-cached data."""
    discrepancies: list[Discrepancy] = []
    bom_part_numbers = set(bom_parts.keys())
    cards_part_numbers = set(cards_data.all_parts.keys())
    fuzzy_matched_count = 0

    if fuzzy_matched_pairs is None:
        fuzzy_matched_pairs = {}
    if cards_norm_set is None:
        cards_norm_set = set()
    if global_names is None:
        global_names = {}

    # Dictionary of original card numbers
    cards_original = cards_data.original_part_numbers

    # 1. Only in BOM — use original format from BOM
    only_in_bom = bom_part_numbers - cards_part_numbers
    if cards_norm_set:
        only_in_bom = {
            p for p in only_in_bom if normalize_part_number(p) not in cards_norm_set
        }

    for part_no in sorted(only_in_bom):
        part = bom_parts[part_no]
        if not is_valid_part_number(part_no):
            continue
        original_part_no = part.part_number if part.part_number else part_no
        discrepancies.append(
            Discrepancy(
                part_number=original_part_no,
                name_cn=part.name_cn,
                name_en=part.name_en,
                qty_bom=part.quantity,
                qty_cards=0.0,
                card_numbers=[],
                discrepancy_type=DiscrepancyType.ONLY_IN_BOM,
                config_name=config_name,
                name_ru=getattr(cards_data, "part_names_ru", {}).get(part_no, ""),
            )
        )

    # 2. Only in cards — show original card number
    only_in_cards = (
        cards_part_numbers - bom_part_numbers - set(fuzzy_matched_pairs.keys())
    )

    for part_no in sorted(only_in_cards):
        if not is_valid_part_number(part_no):
            continue
        qty = cards_data.all_parts[part_no]
        card_numbers = _get_card_numbers(part_no, cards_data)
        name_cn, name_en = global_names.get(part_no, ("", ""))
        original_no = cards_original.get(part_no, part_no)
        discrepancies.append(
            Discrepancy(
                part_number=original_no,
                name_cn=name_cn,
                name_en=name_en,
                qty_bom=0.0,
                qty_cards=qty,
                card_numbers=card_numbers,
                discrepancy_type=DiscrepancyType.ONLY_IN_CARDS,
                config_name=config_name,
                name_ru=getattr(cards_data, "part_names_ru", {}).get(part_no, ""),
            )
        )

    # 3. Fuzzy match — show BOM original
    for cards_pn, bom_pn in sorted(fuzzy_matched_pairs.items()):
        if not is_valid_part_number(cards_pn) or not is_valid_part_number(bom_pn):
            continue
        part = bom_parts.get(bom_pn)
        if part is None:
            continue
        cards_qty = cards_data.all_parts.get(cards_pn, 0.0)
        card_numbers = _get_card_numbers(cards_pn, cards_data)
        fuzzy_matched_count += 1
        original_bom_no = part.part_number if part.part_number else bom_pn
        discrepancies.append(
            Discrepancy(
                part_number=original_bom_no,
                name_cn=part.name_cn,
                name_en=part.name_en,
                qty_bom=part.quantity,
                qty_cards=cards_qty,
                card_numbers=card_numbers,
                discrepancy_type=DiscrepancyType.FUZZY_MATCH,
                config_name=config_name,
                fuzzy_matched_to=bom_pn,
                name_ru=getattr(cards_data, "part_names_ru", {}).get(cards_pn, ""),
            )
        )

    # 4. Quantity mismatch — use original format from BOM
    common_parts = bom_part_numbers & cards_part_numbers
    matched = 0
    for part_no in sorted(common_parts):
        if not is_valid_part_number(part_no):
            continue
        bom_qty = bom_parts[part_no].quantity
        cards_qty = cards_data.all_parts.get(part_no, 0.0)

        if abs(bom_qty - cards_qty) > 0.001:
            part = bom_parts[part_no]
            card_numbers = _get_card_numbers(part_no, cards_data)
            original_part_no = part.part_number if part.part_number else part_no
            discrepancies.append(
                Discrepancy(
                    part_number=original_part_no,
                    name_cn=part.name_cn,
                    name_en=part.name_en,
                    qty_bom=bom_qty,
                    qty_cards=cards_qty,
                    card_numbers=card_numbers,
                    discrepancy_type=DiscrepancyType.QUANTITY_MISMATCH,
                    config_name=config_name,
                    name_ru=getattr(cards_data, "part_names_ru", {}).get(part_no, ""),
                )
            )
        else:
            matched += 1

    # Sort by type priority
    type_order = {
        DiscrepancyType.QUANTITY_MISMATCH: 0,
        DiscrepancyType.ONLY_IN_BOM: 1,
        DiscrepancyType.ONLY_IN_CARDS: 2,
        DiscrepancyType.FUZZY_MATCH: 3,
    }
    discrepancies.sort(
        key=lambda d: (type_order.get(d.discrepancy_type, 99), d.part_number)
    )

    return ConfigComparisonResult(
        config_name=config_name,
        discrepancies=discrepancies,
        total_bom_parts=len(bom_parts),
        total_cards_parts=len(cards_part_numbers),
        matched_parts=matched,
        fuzzy_matched=fuzzy_matched_count,
    )


def _compare_config_worker(
    config_name: str,
    bom_parts_dict: dict[str, tuple[str, str, float, str]],
    cards_all_parts: dict[str, float],
    cards_part_sources: dict[str, list[tuple[str, str, float]]],
    cards_original_part_numbers: dict[str, str],
    fuzzy_matched_pairs: dict[str, str],
    cards_norm_set: set[str],
    global_names_dict: dict[str, tuple[str, str]] = None,
) -> ConfigComparisonResult:
    """Parallel comparison of one configuration (runs in a separate thread)."""
    # Reconstruct PartInfo with original part numbers
    bom_parts: dict[str, PartInfo] = {}
    for pn, (name_cn, name_en, qty, orig_pn) in bom_parts_dict.items():
        bom_parts[pn] = PartInfo(
            part_number=orig_pn,
            name_cn=name_cn,
            name_en=name_en,
            quantity=qty,
        )

    # Minimal CardsData (only all_parts + part_sources + original_part_numbers)
    from app.services.bom_parser_service import CardsData

    minimal_cards = CardsData(
        all_parts=cards_all_parts,
        original_part_numbers=cards_original_part_numbers,
        part_sources=cards_part_sources,
        card_results=[],
        total_cards_processed=0,
    )

    return compare_single_config_cached(
        bom_parts=bom_parts,
        cards_data=minimal_cards,
        config_name=config_name,
        cards_norm_set=cards_norm_set,
        fuzzy_matched_pairs=fuzzy_matched_pairs,
        global_names=global_names_dict,
    )


def compare_all_configs(
    bom: BOMData,
    cards_data: CardsData,
    use_fuzzy: bool = True,
) -> MultiConfigComparisonResult:
    """Compare BOM and cards for ALL configurations simultaneously.

    Builds a shared fuzzy index across all BOM part numbers,
    then performs comparison for each configuration.

    Optimisation: caches the normalised card set and fuzzy pairs
    to avoid recomputing for every configuration.

    Args:
        bom: Parsed BOM data.
        cards_data: Data from operational cards.
        use_fuzzy: Enable approximate comparison.

    Returns:
        MultiConfigComparisonResult with results for all configurations.
    """
    logger.info("Comparing ALL %d configurations...", len(bom.config_names))

    # Build the shared set of all BOM part numbers
    all_bom_parts = set(bom.parts.keys())

    # Create fuzzy matcher
    fuzzy_matcher = FuzzyMatcher(all_bom_parts) if use_fuzzy else None

    # ── Caching: pre-compute all card data ──
    cards_part_numbers = set(cards_data.all_parts.keys())
    cards_norm_set: set = set()
    if fuzzy_matcher:
        cards_norm_set = {fuzzy_matcher.get_normalized(p) for p in cards_part_numbers}

    # Pre-compute fuzzy matching (once for all configurations)
    fuzzy_matched_pairs: dict[str, str] = {}
    if fuzzy_matcher:
        for cards_pn in cards_part_numbers:
            match = fuzzy_matcher.find_fuzzy_match(cards_pn)
            if match and match != cards_pn:
                fuzzy_matched_pairs[cards_pn] = match
                logger.debug("Fuzzy match: '%s' -> '%s'", cards_pn, match)

    # Pre-build PartInfo for all configurations (single pass)
    # IMPORTANT: PartInfo.part_number = original format from BOM (with dashes, etc.)
    config_bom_parts: dict[str, dict[str, PartInfo]] = {}
    # Global names dict (all BOM part numbers, not just this configuration)
    global_names = bom.global_names or {}
    for config_name in bom.config_names:
        parts_for_config: dict[str, PartInfo] = {}
        for part_no, qty in bom.config_quantities[config_name].items():
            if part_no in bom.parts:
                parts_for_config[part_no] = PartInfo(
                    part_number=bom.parts[part_no].part_number,  # ← ORIGINAL format!
                    name_cn=bom.parts[part_no].name_cn,
                    name_en=bom.parts[part_no].name_en,
                    quantity=qty,
                )
        config_bom_parts[config_name] = parts_for_config

    config_results: list[ConfigComparisonResult] = []
    all_discrepancies: list[Discrepancy] = []
    total_configs = len(bom.config_names)

    # ── Parallel comparison of all configurations ──
    if total_configs > 1:
        workers = min(os.cpu_count() or 4, total_configs)
        logger.info(
            "  Parallel comparison: %d threads for %d configurations",
            workers,
            total_configs,
        )

        # Serialize PartInfo to plain dict for thread workers
        # Include original part number as the 4th tuple element
        serialized_configs: dict[str, dict[str, tuple[str, str, float, str]]] = {}
        for cn, parts in config_bom_parts.items():
            serialized_configs[cn] = {
                pn: (p.name_cn, p.name_en, p.quantity, p.part_number)
                for pn, p in parts.items()
            }

        # Extract only needed card data (excluding card_results — ~80% serialisation savings)
        cards_all_parts = cards_data.all_parts
        cards_part_sources = cards_data.part_sources
        cards_original_part_numbers = cards_data.original_part_numbers

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for i, config_name in enumerate(bom.config_names):
                future = executor.submit(
                    _compare_config_worker,
                    config_name=config_name,
                    bom_parts_dict=serialized_configs[config_name],
                    cards_all_parts=cards_all_parts,
                    cards_part_sources=cards_part_sources,
                    cards_original_part_numbers=cards_original_part_numbers,
                    fuzzy_matched_pairs=fuzzy_matched_pairs,
                    cards_norm_set=cards_norm_set,
                    global_names_dict=global_names,
                )
                futures[future] = i

            # Collect results preserving order
            results_by_index: dict[int, ConfigComparisonResult] = {}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result = future.result()
                    results_by_index[idx] = result
                except Exception as e:
                    logger.error(
                        "Error comparing configuration %d: %s", idx + 1, e
                    )  # Restore original order
            for i in range(total_configs):
                if i in results_by_index:
                    result = results_by_index[i]
                    config_results.append(result)
                    all_discrepancies.extend(result.discrepancies)

            if (total_configs - 1) % 5 != 0:
                logger.info(
                    "  ... processed %d/%d configurations", total_configs, total_configs
                )
    else:
        # Single configuration — sequential (faster without thread overhead)
        for i, config_name in enumerate(bom.config_names):
            logger.info(
                "Comparing configuration %d/%d: %s...",
                i + 1,
                total_configs,
                config_name[:50],
            )
            bom_parts_for_config = config_bom_parts[config_name]

            result = compare_single_config_cached(
                bom_parts_for_config,
                cards_data,
                config_name=config_name,
                cards_norm_set=cards_norm_set,
                fuzzy_matched_pairs=fuzzy_matched_pairs,
                global_names=global_names,
            )
            config_results.append(result)
            all_discrepancies.extend(result.discrepancies)

    logger.info("Multi-config comparison complete: %d configurations", total_configs)
    logger.info("  Total discrepancies: %d", len(all_discrepancies))

    return MultiConfigComparisonResult(
        config_results=config_results,
        all_discrepancies=all_discrepancies,
        total_configs=total_configs,
        total_bom_unique_parts=len(all_bom_parts),
        total_cards_unique_parts=len(cards_data.all_parts),
    )


def _get_card_numbers(part_no: str, cards_data: CardsData) -> list[str]:
    """Get unique card numbers where a part appears."""
    sources = cards_data.part_sources.get(part_no, [])
    seen: set[str] = set()
    card_nums: list[str] = []
    for card_number, _, _ in sources:
        if card_number not in seen:
            seen.add(card_number)
            card_nums.append(card_number)
    return card_nums


# ─── Report formatting ──────────────────────────────────────────────────


def format_discrepancy_report(result) -> str:
    """Generate a text discrepancy report.

    Supports both the legacy ComparisonResult and the newer MultiConfigComparisonResult.
    """
    if isinstance(result, MultiConfigComparisonResult):
        return _format_multi_config_report(result)
    else:
        return _format_single_config_report(result)


def _format_single_config_report(comparison) -> str:
    """Format for a single configuration (human-readable)."""
    sep = "─" * 78
    lines = [
        "",
        sep,
        "  ОТЧЁТ ПРОВЕРКИ КОМПЛЕКТАЦИЙ",
        sep,
        "",
        f"  Configuration: {comparison.config_name}",
        f"  Деталей в спецификации: {comparison.total_bom_parts}",
        f"  Деталей в инструкциях: {comparison.total_cards_parts}",
        f"  Совпало:               {comparison.matched_parts}",
        f"  Несоответствий:         {len(comparison.discrepancies)}",
        "",
    ]

    if not comparison.discrepancies:
        lines.append("  ✓ ✓ Несоответствий не найдено.")
        lines.append(sep)
        return "\n".join(lines)

    for dtype in [
        DiscrepancyType.QUANTITY_MISMATCH,
        DiscrepancyType.ONLY_IN_BOM,
        DiscrepancyType.ONLY_IN_CARDS,
    ]:
        type_disc = [d for d in comparison.discrepancies if d.discrepancy_type == dtype]
        if not type_disc:
            continue
        lines.append(f"  {dtype} — {len(type_disc)} items:")
        lines.append(f"  {'Деталь':<20} {'В спец.':<10} {'В инстр.':<11}")
        for d in type_disc:
            lines.append(
                f"  {d.part_number:<20} {d.qty_bom:<10.1f} {d.qty_cards:<11.1f}"
            )
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def _format_multi_config_report(result: MultiConfigComparisonResult) -> str:
    """Plain-language report format for production workers."""
    sep = "─" * 78

    lines = [
        "",
        sep,
        "  ОТЧЁТ ПРОВЕРКИ КОМПЛЕКТАЦИЙ",
        sep,
        "",
        f"  Всего проверено комплектаций: {result.total_configs}",
        f"  Деталей в BOM:              {result.total_bom_unique_parts}",
        f"  Деталей в операционных картах: {result.total_cards_unique_parts}",
        "",
    ]

    total = len(result.all_discrepancies)
    if total == 0:
        lines.append("  ✓ Расхождений не найдено — всё совпадает.")
        lines.append("")
        lines.append(sep)
        return "\n".join(lines)

    qty_mismatch = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
    )
    only_bom = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.ONLY_IN_BOM
    )
    only_cards = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.ONLY_IN_CARDS
    )

    lines.append(f"  Найдено несоответствий: {total}")
    lines.append(f"    • Разное количество:         {qty_mismatch}")
    lines.append(f"    • Есть в BOM, нет в операционных картах: {only_bom}")
    lines.append(f"    • Есть в операционных картах, нет в BOM: {only_cards}")
    lines.append("")

    # Configuration summary — compact
    lines.append("  КРАТКАЯ СВОДКА ПО КОМПЛЕКТАЦИЯМ:")
    lines.append(f"  {'№':<3} {'Matched':<8} {'Discrp.':<10} Config name")

    config_to_id = {}
    for i, cr in enumerate(result.config_results, 1):
        cid = f"№{i}"
        config_to_id[cr.config_name] = cid
        short = (
            cr.config_name if len(cr.config_name) <= 53 else cr.config_name[:50] + "..."
        )
        lines.append(
            f"  {i:<3} {cr.matched_parts:<8} {len(cr.discrepancies):<10} {short}"
        )

    lines.append("")
    lines.append("  ПОДРОБНОСТИ (первые 30 позиций по типу):")
    lines.append("")

    # Grouping by type — unique parts with configuration numbers
    type_order = [
        (DiscrepancyType.QUANTITY_MISMATCH, "РАЗНОЕ КОЛИЧЕСТВО"),
        (DiscrepancyType.ONLY_IN_BOM, "ЕСТЬ В BOM, НЕТ В КАРТАХ"),
        (DiscrepancyType.ONLY_IN_CARDS, "ЕСТЬ В КАРТАХ, НЕТ В BOM"),
    ]

    for dtype, label in type_order:
        type_disc = [d for d in result.all_discrepancies if d.discrepancy_type == dtype]
        if not type_disc:
            continue

        # Group by part
        part_groups = {}
        for d in type_disc:
            pn = d.part_number
            if pn not in part_groups:
                part_groups[pn] = {
                    "bom": d.qty_bom,
                    "cards": d.qty_cards,
                    "configs": set(),
                    "name": d.name_cn,
                }
            if d.config_name in config_to_id:
                part_groups[pn]["configs"].add(config_to_id[d.config_name])

        sorted_parts = sorted(part_groups.keys())

        lines.append(f"  ── {label}: {len(type_disc)} entries ──")

        if dtype == DiscrepancyType.QUANTITY_MISMATCH:
            lines.append(
                f"  {'Деталь':<20} {'В BOM':<10} {'In cards':<11} Комплектации"
            )
        elif dtype == DiscrepancyType.ONLY_IN_BOM:
            lines.append(f"  {'Деталь':<20} {'В BOM':<10} Комплектации")
        else:
            lines.append(f"  {'Деталь':<20} {'In cards':<11} Комплектации")

        for pn in sorted_parts[:30]:
            g = part_groups[pn]
            c_str = ",".join(
                sorted(g["configs"], key=lambda x: int(x[1:]) if x[1:].isdigit() else 0)
            )
            if dtype == DiscrepancyType.QUANTITY_MISMATCH:
                lines.append(
                    f"  {pn:<20} {g['bom']:<10.1f} {g['cards']:<11.1f} {c_str}"
                )
            elif dtype == DiscrepancyType.ONLY_IN_BOM:
                lines.append(f"  {pn:<20} {g['bom']:<10.1f} {c_str}")
            else:
                lines.append(f"  {pn:<20} {g['cards']:<11.1f} {c_str}")

        if len(sorted_parts) > 30:
            lines.append(f"  ... и ещё {len(sorted_parts) - 30} деталей")
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


# ─── Service ──────────────────────────────────────────────────────────────────


@dataclass
class IntegrityCheck:
    """Data integrity check result for the comparison.

    Verifies that every BOM part is accounted for:
    either matched with cards or recorded as a discrepancy.

    Attributes:
        is_ok: True if all checks pass.
        total_configs: Number of configurations checked.
        configs_ok: Number of configurations that passed.
        config_issues: List of issues per configuration.
        global_issue: Global issue (if any).
        details_by_config: Details per configuration.
    """

    is_ok: bool = True
    total_configs: int = 0
    configs_ok: int = 0
    config_issues: list[str] = field(default_factory=list)
    global_issue: str = ""
    details_by_config: list[dict[str, object]] = field(default_factory=list)


def verify_integrity(result: MultiConfigComparisonResult) -> IntegrityCheck:
    """Verify integrity of comparison results.

    Checks two conditions:
      1. Per configuration:
         matched_parts + ONLY_IN_BOM + QUANTITY_MISMATCH + FUZZY_MATCH == total_bom_parts
         (ONLY_IN_CARDS excluded — those are card parts missing from BOM)
      2. Globally: sum of all discrepancy types == total discrepancy count

    Args:
        result: Multi-configuration comparison result.

    Returns:
        IntegrityCheck with all verification results.
    """
    total_discrepancies = len(result.all_discrepancies)
    configs_ok = 0
    config_issues: list[str] = []
    details_by_config: list[dict[str, object]] = []

    # Check 1: per configuration
    for cr in result.config_results:
        only_bom_count = sum(
            1
            for d in cr.discrepancies
            if d.discrepancy_type == DiscrepancyType.ONLY_IN_BOM
        )
        qty_mismatch_count = sum(
            1
            for d in cr.discrepancies
            if d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
        )
        fuzzy_count = sum(
            1
            for d in cr.discrepancies
            if d.discrepancy_type == DiscrepancyType.FUZZY_MATCH
        )
        accounted_bom = (
            cr.matched_parts + only_bom_count + qty_mismatch_count + fuzzy_count
        )
        expected = cr.total_bom_parts

        config_ok = accounted_bom == expected
        if config_ok:
            configs_ok += 1
        else:
            diff = accounted_bom - expected
            issue = (
                f"{cr.config_name[:50]}: учтено {accounted_bom}, "
                f"ожидалось {expected} (diff={diff})"
            )
            config_issues.append(issue)

        details_by_config.append(
            {
                "config_name": cr.config_name,
                "is_ok": config_ok,
                "total_bom_parts": expected,
                "accounted": accounted_bom,
                "diff": accounted_bom - expected,
                "matched": cr.matched_parts,
                "only_in_bom": only_bom_count,
                "qty_mismatch": qty_mismatch_count,
                "fuzzy_match": fuzzy_count,
            }
        )

    # Check 2: global — sum of types == total count
    qty_m = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.QUANTITY_MISMATCH
    )
    only_b = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.ONLY_IN_BOM
    )
    only_c = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.ONLY_IN_CARDS
    )
    fuzzy_c = sum(
        1
        for d in result.all_discrepancies
        if d.discrepancy_type == DiscrepancyType.FUZZY_MATCH
    )
    sum_check = qty_m + only_b + only_c + fuzzy_c

    global_issue = ""
    if sum_check != total_discrepancies:
        global_issue = (
            f"Discrepancy type sum ({sum_check}) "
            f"does not equal total count ({total_discrepancies})"
        )

    is_ok = len(config_issues) == 0 and global_issue == ""

    return IntegrityCheck(
        is_ok=is_ok,
        total_configs=len(result.config_results),
        configs_ok=configs_ok,
        config_issues=config_issues,
        global_issue=global_issue,
        details_by_config=details_by_config,
    )


class MatchingEngine:
    """BOM-to-cards comparison service.

    Designed for server architecture (FastAPI + SQLite + Redis).
    Supports both single and multi-configuration comparison.
    """

    def __init__(self, use_fuzzy: bool = True):
        self.use_fuzzy = use_fuzzy

    def compare(
        self,
        bom: BOMData,
        cards_data: CardsData,
        single_config: str | None = None,
    ) -> MultiConfigComparisonResult:
        """Run comparison."""
        if single_config:
            # Single configuration comparison — use original number format from BOM
            bom_parts: dict[str, PartInfo] = {}
            if single_config in bom.config_quantities:
                for part_no, qty in bom.config_quantities[single_config].items():
                    if part_no in bom.parts:
                        bom_parts[part_no] = PartInfo(
                            part_number=bom.parts[part_no].part_number,  # ← original!
                            name_cn=bom.parts[part_no].name_cn,
                            name_en=bom.parts[part_no].name_en,
                            quantity=qty,
                        )

            all_bom_parts = set(bom.parts.keys())
            fuzzy_matcher = FuzzyMatcher(all_bom_parts) if self.use_fuzzy else None
            single_result = compare_single_config(
                bom_parts,
                cards_data,
                config_name=single_config,
                fuzzy_matcher=fuzzy_matcher,
            )
            return MultiConfigComparisonResult(
                config_results=[single_result],
                all_discrepancies=list(single_result.discrepancies),
                total_configs=1,
                total_bom_unique_parts=len(all_bom_parts),
                total_cards_unique_parts=len(cards_data.all_parts),
            )

        return compare_all_configs(bom, cards_data, use_fuzzy=self.use_fuzzy)
