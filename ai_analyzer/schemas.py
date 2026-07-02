import os
from typing import Literal

from pydantic import BaseModel, Field

LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7B")

########################################################

""" Запрос """


class AnalyzeStructureRequest(BaseModel):
    bom: list[dict[str, object]] = Field(
        description="Массив представительских JSON-слепков BOM-файлов"
    )
    sample_cards: list[dict[str, object]] = Field(
        description="Массив представительских JSON-слепков операционных карт"
    )
    options: dict[str, object] | None = None


########################################################


########################################################
""" BOM: анализ """


class ColumnMapping(BaseModel):
    col_index: int = Field(
        ge=0,
        description="1-based column index (0 = not found)",
    )
    header: str | None = Field(
        None,
        description="Column header text if found (null if no header)",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model confidence score from 0.0 to 1.0",
    )


class ConfigColumnMapping(ColumnMapping):
    type: Literal["config", "vin_split"] = Field(description="Тип колонки комплектации")


class BomColumns(BaseModel):
    part_no: ColumnMapping = Field(description="Колонка номера детали (part number)")
    qty: ColumnMapping = Field(description="Колонка количества (quantity)")
    name_cn: ColumnMapping = Field(description="Колонка с китайским наименованием")
    name_en: ColumnMapping | None = Field(
        None, description="Колонка с английским наименованием, если есть"
    )
    config_columns: list[ConfigColumnMapping] = Field(
        default=[], description="Список дополнительных колонок конфигурации"
    )


class BomLayout(BaseModel):
    type: Literal["single_table", "multi_block", "service_sheet"]
    description: str


class BomSheet(BaseModel):
    sheet_name: str = Field(description="Sheet name in the Excel file")
    sheet_type: Literal["bom_data", "service", "unknown"] = Field(
        description="Sheet type: data, service, or unknown"
    )
    header_rows: list[int] = Field(
        description="List of row indices occupied by the header"
    )
    data_start_row: int = Field(
        ge=1,
        description="Row index where actual data starts (1-based)",
    )
    total_data_rows_estimate: int = Field(
        ge=0,
        description="Estimated number of data rows",
    )
    columns: BomColumns = Field(description="Key column mappings for the BOM sheet")
    layout: BomLayout = Field(description="Meta layout description of the sheet")


class BomAnalysisResult(BaseModel):
    sheets: list[BomSheet]


########################################################


########################################################

""" Операционные карты: анализ """


class MultiCardConfig(BaseModel):
    separator_type: Literal["empty_row", "marker", "empty_row_or_marker"]
    empty_rows_separator: int | None = 1
    card_end_markers: list[str] | None = None
    has_repeating_header: bool
    parts_header_row: int | None = None
    parts_data_start_row: int | None = None
    max_cards: int | None = 0


class TableBoundaries(BaseModel):
    type: Literal[
        "end_markers", "empty_rows", "next_header", "fixed_count", "multi_card"
    ] = Field(description="Как определять границы таблицы")
    markers: list[str] | None = Field(
        None,
        description="Список слов-маркеров (например, 'Итого', 'Проверил') для конца таблицы",
    )
    empty_rows_threshold: int | None = Field(
        None, description="Порог пустых строк для завершения парсинга"
    )
    fixed_count: int | None = Field(
        None, description="Жесткое количество строк (если есть)"
    )
    multi_card: MultiCardConfig | None = Field(
        None, description="Конфигурация для multi_card"
    )


class CardColumns(BaseModel):
    part_no: ColumnMapping
    name_cn: ColumnMapping
    name_en: ColumnMapping | None = None
    qty: ColumnMapping


class CardSheetMapping(BaseModel):
    sheet_name: str | None = Field(
        description="Sheet name (null if rule applies to all sheets)"
    )
    sheet_type: Literal["card_data", "service", "unknown"] = Field(
        description="Type of content on the sheet"
    )
    header_rows: list[int] = Field(description="Header row indices (1-based)")
    data_start_row: int = Field(
        ge=1,
        description="Data start row (1-based)",
    )
    columns: CardColumns
    table_boundaries: TableBoundaries


class CardFormatMapping(BaseModel):
    structure_type: Literal["standard_table", "graphic_number", "inspection", "unknown"]
    description: str
    card_number_source: Literal["filename", "sheet_content", "header", "cell"]
    card_number_pattern: str
    card_number_confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in card number detection",
    )
    sheets: list[CardSheetMapping]


class ClassificationPattern(BaseModel):
    type: Literal["filename_regex", "filename_keyword", "sheet_keyword"] = Field(
        description="Тип правила классификации"
    )
    pattern: str | None = Field(
        None, description="Регулярное выражение (если type='filename_regex')"
    )
    keywords: list[str] | None = Field(
        None, description="Список ключевых слов (для keyword-типов)"
    )
    format_group: str | None = None


class FileClassificationRules(BaseModel):
    operational_card_patterns: list[ClassificationPattern] = Field(
        description="Паттерны для определения операционных карт"
    )
    service_file_patterns: list[ClassificationPattern] = Field(
        description="Паттерны для определения служебных файлов (обложки, оглавления)"
    )


class CardAnalysisResult(BaseModel):
    formats: dict[str, CardFormatMapping] = Field(
        description="Форматы карт, сгруппированные по названиям групп"
    )
    file_classification_rules: FileClassificationRules


########################################################


########################################################

""" Маппинг"""


class FieldMapping(BaseModel):
    bom_column: str = Field(
        description="Column key from BOM structure (e.g. 'part_no')"
    )
    card_column: str = Field(
        description="Column key from card structure (e.g. 'part_no')"
    )
    match_type: Literal["exact", "fuzzy", "regex"] = Field(
        description="Type of matching algorithm"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score from 0.0 to 1.0",
    )


class BomToCardMapping(BaseModel):
    part_no: FieldMapping = Field(description="Маппинг для номера детали")
    name: FieldMapping = Field(description="Маппинг для наименования")
    quantity: FieldMapping = Field(description="Маппинг для количества")


class MappingResult(BaseModel):
    bom_to_card: BomToCardMapping = Field(
        description="Итоговый маппинг полей BOM на поля операционной карты"
    )


########################################################
