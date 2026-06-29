from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CardSheetInfo:
    """Информация об одном листе операционной карты."""

    card_number: str
    sheet_name: str
    operation_name: str = ""
    is_valid: bool = False
    has_data: bool = False
    max_data_row: int = 0  # Максимальное количество строк данных на листе (для защиты от ложного вертикального split)


@dataclass
class CardPart:
    """Деталь, найденная в операционной карте."""

    part_number: str
    quantity: float
    source_card: str
    source_sheet: str
    name_ru: str = ""


@dataclass
class CardParseResult:
    """Результат парсинга одной операционной карты."""

    card_number: str
    file_path: str
    sheets: list[CardSheetInfo]
    parts: list[CardPart]
    aggregated_parts: dict[str, float]  # part_number -> total_qty
    is_service_file: bool = False  # True если файл был определён как служебный
    tables_extracted: int = 0  # Количество таблиц (операций) найденных во всех листах


@dataclass
class CardsData:
    """Результат парсинга всех операционных карт."""

    all_parts: dict[str, float]  # part_number -> суммарное количество
    original_part_numbers: dict[str, str] = field(
        default_factory=dict
    )  # cleaned_part_no -> оригинальный (с тире и т.д.)
    part_names_ru: dict[str, str] = field(default_factory=dict)
    part_sources: dict[str, list[tuple[str, str, float]]] = field(default_factory=dict)
    card_results: list[CardParseResult] = field(default_factory=list)
    total_cards_processed: int = 0
    total_sheets_processed: int = 0
    total_sheets_skipped: int = 0
    service_files_skipped: int = 0
    corrupted_files: list[str] = field(default_factory=list)
    corrupted_files_detailed: list[dict[str, str]] = field(default_factory=list)
    total_tables_extracted: int = 0  # Количество таблиц (операций) во всех листах
    split_stats: Any = None
