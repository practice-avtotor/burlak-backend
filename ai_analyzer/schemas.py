from typing import Literal
from pydantic import BaseModel, Field

LLM_MODEL = "qwen2.5:7B"

########################################################

""" Запрос """

class AnalyzeStructureRequest(BaseModel):
    bom: dict
    sample_cards: list[dict]
    options: dict | None = None

########################################################



########################################################
""" BOM: анализ """

class ColumnMapping(BaseModel):
    col_index: int = Field(description="Индекс колонки в таблице (0-based)")
    header: str | None = Field(None, description="Текст заголовка колонки, если найден")
    confidence: float = Field(description="Оценка уверенности модели от 0.0 до 1.0")


class ConfigColumn(ColumnMapping):
    type: str = Field(description="Тип конфигурационного параметра (например, 'color', 'option')")


class BomColumns(BaseModel):
    part_no: ColumnMapping = Field(description="Колонка номера детали (part number)")
    qty: ColumnMapping = Field(description="Колонка количества (quantity)")
    name_cn: ColumnMapping = Field(description="Колонка с китайским наименованием")
    name_en: ColumnMapping | None = Field(None, description="Колонка с английским наименованием, если есть")
    config_columns: list[ConfigColumn] = Field(default=[], description="Список дополнительных колонок конфигурации")


class BomLayout(BaseModel):
    has_merged_cells: bool = Field(description="Флаг наличия объединенных ячеек в шапке")
    header_alignment: Literal["horizontal", "vertical", "mixed"] = Field(description="Ориентация шапки таблицы")


class BomSheet(BaseModel):
    sheet_name: str = Field(description="Имя листа в Excel-файле")
    sheet_type: Literal["data", "service", "unknown"] = Field(description="Тип листа: данные, служебный или неизвестный")
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

class TableBoundaries(BaseModel):
    type: Literal["end_markers", "empty_rows", "next_header", "fixed_count"] = Field(description="Как определять границы таблицы")
    markers: list[str] | None = Field(None, description="Список слов-маркеров (например, 'Итого', 'Проверил') для конца таблицы")
    empty_rows_threshold: int | None = Field(None, description="Порог пустых строк для завершения парсинга")
    fixed_count: int | None = Field(None, description="Жесткое количество строк (если есть)")


class CardColumns(BaseModel):
    part_no: ColumnMapping
    name_cn: ColumnMapping
    name_en: ColumnMapping | None = None
    qty: ColumnMapping


class CardSheetMapping(BaseModel):
    sheet_name: str | None = Field(description="Имя листа. null, если правило применимо ко всем листам")
    sheet_type: Literal["card_data", "service", "unknown"] = Field(description="Тип содержимого на листе")
    header_rows: list[int] = Field(description="Строки заголовков (1-based)")
    data_start_row: int = Field(description="Строка начала данных")
    columns: CardColumns
    table_boundaries: TableBoundaries


class ClassificationPattern(BaseModel):
    type: Literal["filename_regex", "filename_keyword", "sheet_keyword"] = Field(description="Тип правила классификации")
    pattern: str | None = Field(None, description="Регулярное выражение (если type='filename_regex')")
    keywords: list[str] | None = Field(None, description="Список ключевых слов (для keyword-типов)")


class FileClassificationRules(BaseModel):
    operational_card_patterns: list[ClassificationPattern] = Field(description="Паттерны для определения операционных карт")
    service_file_patterns: list[ClassificationPattern] = Field(description="Паттерны для определения служебных файлов (обложки, оглавления)")


class CardAnalysisResult(BaseModel):
    structure_type: Literal["standard_table", "graphic_number", "inspection", "unknown"] = Field(description="Базовый тип структуры карты")
    description: str = Field(description="Человекочитаемое описание структуры")
    card_number_source: Literal["filename", "sheet_content", "header", "cell"] = Field(description="Откуда парсеру брать номер карты")
    card_number_pattern: str = Field(description="Паттерн для извлечения номера карты (Regex)")
    card_number_confidence: float = Field(description="Уверенность в способе извлечения номера")
    sheets: list[CardSheetMapping] = Field(description="Описание маппинга для листов карты")
    file_classification_rules: FileClassificationRules = Field(description="Правила классификации файлов в ZIP-архиве")

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
    name_cn: FieldMapping = Field(description="Маппинг для наименования (китайский/основной)")
    name_en: FieldMapping | None = Field(None, description="Маппинг для наименования (английский), если применимо")
    qty: FieldMapping = Field(description="Маппинг для количества")


class MappingResult(BaseModel):
    bom_to_card: BomToCardMapping = Field(description="Итоговый маппинг полей BOM на поля операционной карты")

########################################################

