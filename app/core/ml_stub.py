"""ML Service stub for local development — embedded in backend container.

Provides mock responses for:
  - POST /api/v1/analyze-structure: structural analysis of BOM and card XLSX files
  - POST /api/v1/translate: batch translation of Chinese text to Russian

Used via ``docker-compose.dev.yml`` profile ``mock``.
"""

from typing import Any

from fastapi import FastAPI

app = FastAPI(title="ML Service Mock Stub")


@app.post("/api/v1/analyze-structure")
async def analyze_structure(payload: dict[str, Any]) -> dict[str, Any]:
    """Mock structure analysis endpoint returning a default mapping config.

    Accepts: {"bom": [...], "sample_cards": [...], "options": {...}}
    Returns: {"status": "success", "mapping_config": {...}}
    """
    return {
        "status": "success",
        "mapping_config": {
            "bom": {
                "sheets": [
                    {
                        "sheet_name": "总装BOM",
                        "sheet_type": "bom_data",
                        "header_rows": [1],
                        "data_start_row": 2,
                        "columns": {
                            "part_no": {"col_index": 2},
                            "name_cn": {"col_index": 3},
                            "name_en": {"col_index": 4},
                            "qty": {"col_index": 5},
                        },
                    }
                ]
            },
            "cards": {
                "formats": {
                    "card_format_A": {
                        "structure_type": "standard_table",
                        "description": "Технологические карты 工艺卡片. Один файл содержит несколько карт, разделённых пустой строкой.",
                        "card_number_source": "cell",
                        "card_number_pattern": "CM-[A-Z0-9]+",
                        "card_number_confidence": 0.90,
                        "sheets": [
                            {
                                "sheet_name": None,
                                "sheet_type": "card_data",
                                "header_rows": [1],
                                "data_start_row": 2,
                                "columns": {
                                    "part_no": {
                                        "col_index": 18,
                                        "header": "零部件代号",
                                        "confidence": 0.95,
                                    },
                                    "name_cn": {
                                        "col_index": 0,
                                        "header": None,
                                        "confidence": 0.0,
                                    },
                                    "qty": {
                                        "col_index": 0,
                                        "header": None,
                                        "confidence": 0.0,
                                    },
                                },
                                "table_boundaries": {
                                    "type": "multi_card",
                                    "multi_card": {
                                        "separator_type": "empty_row",
                                        "empty_rows_separator": 1,
                                        "has_repeating_header": True,
                                        "parts_header_row": 1,
                                        "parts_data_start_row": 2,
                                        "max_cards": 0,
                                    },
                                },
                            }
                        ],
                    }
                },
                "file_classification_rules": {
                    "operational_card_patterns": [
                        {
                            "type": "filename_regex",
                            "pattern": r"^[A-Za-z0-9]+-[A-Za-z0-9]*-AS-\d+",
                            "format_group": "card_format_A",
                        },
                        {
                            "type": "filename_regex",
                            "pattern": r"^[A-Za-z]{1,3}\d{2,}",
                            "format_group": "card_format_A",
                        },
                        {
                            "type": "filename_regex",
                            "pattern": r"^\d{2,}",
                            "format_group": "card_format_A",
                        },
                        {
                            "type": "sheet_keyword",
                            "keywords": [
                                "作业指导书",
                                "作业要领书",
                                "操作指导",
                                "工艺卡",
                                "工序卡",
                            ],
                            "format_group": "card_format_A",
                        },
                    ],
                    "service_file_patterns": [
                        {
                            "type": "filename_keyword",
                            "keywords": [
                                "封面",
                                "目录",
                                "记录表",
                                "空表",
                                "填写范本",
                                "填写说明",
                                "工时汇总",
                                "对比",
                            ],
                        },
                        {
                            "type": "filename_keyword",
                            "keywords": [
                                "обложка",
                                "содержание",
                                "cover",
                                "toc",
                                "template",
                            ],
                        },
                    ],
                },
            },
            "mapping": {
                "bom_to_card": {
                    "part_no": {
                        "bom_column": "part_no",
                        "card_column": "part_no",
                        "match_type": "exact",
                        "confidence": 0.95,
                    },
                    "name": {
                        "bom_column": "name_cn",
                        "card_column": "name_cn",
                        "match_type": "fuzzy",
                        "confidence": 0.90,
                    },
                    "quantity": {
                        "bom_column": "qty",
                        "card_column": "qty",
                        "match_type": "exact",
                        "confidence": 0.97,
                    },
                },
            },
        },
    }


@app.post("/api/v1/translate")
async def translate(payload: dict[str, Any]) -> dict[str, Any]:
    """Mock translation endpoint that prefixes translated texts.

    Accepts: {"texts": [...], "source_lang": "zh", "target_lang": "ru"}
    Returns: {"translations": [...]}
    """
    texts: list[str] = payload.get("texts", [])
    translations = [f"[MOCK] {t}" for t in texts]
    return {"translations": translations}
