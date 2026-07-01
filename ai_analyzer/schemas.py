from typing import Literal

from pydantic import BaseModel, Field

LLM_MODEL = "qwen2.5:7B"

########################################################

""" Запрос """

class AnalyzeStructureRequest(BaseModel):
    bom: list[dict[str, object]] = Field(description="Массив представительских JSON-слепков BOM-файлов")
    sample_cards: list[dict[str, object]] = Field(description="Массив представительских JSON-слепков операционных карт")
    options: dict[str, object] | None = None

########################################################



########################################################
""" BOM: анализ """

class ColumnMapping(BaseModel):
    col_index: int = Field(description="1-based номер колонки (0 = не найдена)")
    header: str | None = Field(None, description="Текст заголовка колонки, если найден (null если заголовка нет)")
    confidence: float = Field(description="Оценка уверенности модели от 0.0 до 1.0")


class ConfigColumnMapping(ColumnMapping):
    type: Literal["config", "vin_split"] = Field(description="Тип колонки комплектации")


class BomColumns(BaseModel):
    part_no: ColumnMapping = Field(description="Колонка номера детали (part number)")
    qty: ColumnMapping = Field(description="Колонка количества (quantity)")
    name_cn: ColumnMapping = Field(description="Колонка с китайским наименованием")
    name_en: ColumnMapping | None = Field(None, description="Колонка с английским наименованием, если есть")
    config_columns: list[ConfigColumnMapping] = Field(default=[], description="Список дополнительных колонок конфигурации")

class BomBlock(BaseModel):
    part_no_col: int
    name_col: int
    qty_col: int
    start_col: int
    end_col: int

class BomLayout(BaseModel):
    type: Literal["single_table", "multi_block", "service_sheet"]
    description: str
    blocks: list[BomBlock] | None = None


class BomSheet(BaseModel):
    sheet_name: str = Field(description="Имя листа в Excel-файле")
    sheet_type: Literal["bom_data", "service", "unknown"] = Field(description="Тип листа: данные, служебный или неизвестный")
    header_rows: list[int] = Field(description="Список индексов строк, которые занимает заголовок")
    data_start_row: int = Field(description="Индекс строки, с которой начинаются фактические данные")
    total_data_rows_estimate: int = Field(description="Оценочное количество строк данных")
    columns: BomColumns = Field(description="Разметка ключевых колонок спецификации")
    layout: BomLayout = Field(description="Мета-разметка структуры листа")


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
    type: Literal["end_markers", "empty_rows", "next_header", "fixed_count", "multi_card"] = Field(description="Как определять границы таблицы")
    markers: list[str] | None = Field(None, description="Список слов-маркеров (например, 'Итого', 'Проверил') для конца таблицы")
    empty_rows_threshold: int | None = Field(None, description="Порог пустых строк для завершения парсинга")
    fixed_count: int | None = Field(None, description="Жесткое количество строк (если есть)")
    multi_card: MultiCardConfig | None = Field(None, description="Конфигурация для multi_card")


class CardColumns(BaseModel):
    part_no: ColumnMapping
    name_cn: ColumnMapping
    name_en: ColumnMapping | None = None
    qty: ColumnMapping


class CardSheetMapping(BaseModel):
    sheet_name: str | None = Field(description="Имя листа (null, если правило применимо ко всем листам)")
    sheet_type: Literal["card_data", "service", "unknown"] = Field(description="Тип содержимого на листе")
    header_rows: list[int] = Field(description="Строки заголовков (1-based)")
    data_start_row: int = Field(description="Строка начала данных")
    columns: CardColumns
    table_boundaries: TableBoundaries

class CardFormatMapping(BaseModel):
    structure_type: Literal["standard_table", "graphic_number", "inspection", "unknown"]
    description: str
    card_number_source: Literal["filename", "sheet_content", "header", "cell"]
    card_number_pattern: str
    card_number_confidence: float
    sheets: list[CardSheetMapping]

class ClassificationPattern(BaseModel):
    type: Literal["filename_regex", "filename_keyword", "sheet_keyword"] = Field(description="Тип правила классификации")
    pattern: str | None = Field(None, description="Регулярное выражение (если type='filename_regex')")
    keywords: list[str] | None = Field(None, description="Список ключевых слов (для keyword-типов)")
    format_group: str | None = None


class FileClassificationRules(BaseModel):
    operational_card_patterns: list[ClassificationPattern] = Field(description="Паттерны для определения операционных карт")
    service_file_patterns: list[ClassificationPattern] = Field(description="Паттерны для определения служебных файлов (обложки, оглавления)")


class CardAnalysisResult(BaseModel):
    formats: dict[str, CardFormatMapping] = Field(description="Форматы карт, сгруппированные по названиям групп")
    file_classification_rules: FileClassificationRules

########################################################



########################################################

""" Маппинг"""

class FieldMapping(BaseModel):
    bom_column: str = Field(description="Ключ колонки из структуры BOM (например, 'part_no')")
    card_column: str = Field(description="Ключ колонки из структуры карты (например, 'part_no')")
    match_type: Literal["exact", "fuzzy", "regex"] = Field(description="Тип алгоритма сопоставления")
    confidence: float = Field(description="Оценка уверенности от 0.0 до 1.0")


class BomToCardMapping(BaseModel):
    part_no: FieldMapping = Field(description="Маппинг для номера детали")
    name: FieldMapping = Field(description="Маппинг для наименования")
    quantity: FieldMapping = Field(description="Маппинг для количества")

class MappingResult(BaseModel):
    bom_to_card: BomToCardMapping = Field(description="Итоговый маппинг полей BOM на поля операционной карты")

########################################################

