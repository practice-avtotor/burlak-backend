from __future__ import annotations

from dataclasses import dataclass, field

from app.services.splitter import SplitStatistics


@dataclass
class CardSheetInfo:
    """Information about a single operational card sheet."""

    card_number: str
    sheet_name: str
    operation_name: str = ""
    is_valid: bool = False
    has_data: bool = False
    max_data_row: int = (
        0  # Maximum data rows on this sheet (guards against false vertical split)
    )


@dataclass
class CardPart:
    """A single part found in an operational card."""

    part_number: str
    quantity: float
    source_card: str
    source_sheet: str
    name_ru: str = ""


@dataclass
class CardParseResult:
    """Result of parsing a single operational card."""

    card_number: str
    file_path: str
    sheets: list[CardSheetInfo]
    parts: list[CardPart]
    aggregated_parts: dict[str, float]  # part_number -> total_qty
    is_service_file: bool = False  # True if the file was classified as a service file
    tables_extracted: int = 0  # Number of tables (operations) found across all sheets


@dataclass
class CardsData:
    """Result of parsing all operational cards."""

    all_parts: dict[str, float]  # part_number -> total quantity
    original_part_numbers: dict[str, str] = field(
        default_factory=dict
    )  # cleaned_part_no -> original (with dashes etc.)
    part_names_ru: dict[str, str] = field(default_factory=dict)
    part_sources: dict[str, list[tuple[str, str, float]]] = field(default_factory=dict)
    card_results: list[CardParseResult] = field(default_factory=list)
    total_cards_processed: int = 0
    total_sheets_processed: int = 0
    total_sheets_skipped: int = 0
    service_files_skipped: int = 0
    corrupted_files: list[str] = field(default_factory=list)
    corrupted_files_detailed: list[dict[str, str]] = field(default_factory=list)
    total_tables_extracted: int = 0  # Number of tables (operations) across all sheets
    split_stats: SplitStatistics | None = None
