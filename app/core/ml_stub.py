from typing import Any

from fastapi import FastAPI, Request

app = FastAPI(title="ML Service Mock Stub")


@app.post("/api/v1/analyze-structure")
async def analyze_structure(request: Request) -> dict[str, Any]:
    """Mock structure analysis endpoint returning a default mapping config."""
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
                "table_boundaries": {
                    "header_row": 1,
                    "data_start_row": 2,
                    "end_markers": ["签字", "审核"],
                },
                "columns": {
                    "part_no": 1,
                    "qty": 3,
                    "name": 2,
                },
                "sheets": {
                    "default_type": "operational",
                },
            },
        },
    }


@app.post("/api/v1/translate")
async def translate(payload: dict[str, Any]) -> dict[str, Any]:
    """Mock translation endpoint that prefixes translated texts."""
    texts = payload.get("texts", [])
    translations = [f"Translated {t}" for t in texts]
    return {"translations": translations}
