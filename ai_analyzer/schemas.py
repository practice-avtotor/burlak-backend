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

class CardAnalysisResult(BaseModel):
    structure_type: str
    description: str
    card_number_source: str
    card_number_pattern: str
    card_number_confidence: float
    sheets: list[dict]
    file_classification_rules: dict

########################################################



########################################################

""" Маппинг"""

class FieldMapping(BaseModel):
    bom_column: str
    card_column: str
    match_type: str
    confidence: float


class MappingResult(BaseModel):
    bom_to_card: dict[str, FieldMapping]

########################################################

