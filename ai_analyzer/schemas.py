from pydantic import BaseModel


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
    col_index: int
    header: str | None
    confidence: float


class ConfigColumn(ColumnMapping):
    type: str


class BomSheet(BaseModel):
    sheet_name: str
    sheet_type: str
    header_rows: list[int]
    data_start_row: int
    total_data_rows_estimate: int
    columns: dict
    layout: dict


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

