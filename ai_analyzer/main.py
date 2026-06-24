from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from schemas import AnalyzeStructureRequest
from pipeline import StructurePipeline
from services import LLMAnalysisError

app = FastAPI(title="ML Structure Analysis Service")

# Регистрируем глобальный обработчик ошибки
@app.exception_handler(LLMAnalysisError)
async def llm_analysis_error_handler(request: Request, exc: LLMAnalysisError):
    return JSONResponse(
        status_code=500, # Используем 500, так как ошибка на стороне анализатора
        content={
            "status": "error",
            "error": {
                "code": exc.code,
                "message": exc.message,
                "detail": exc.details
            }
        }
    )

# Валидация JSON в AnalyzeStructureRequest происходит автоматически
@app.post("/api/v1/analyze-structure")
async def analyze_structure(request: AnalyzeStructureRequest):
    pipeline = StructurePipeline()
    result = await pipeline.run(
        bom_snapshot=request.bom,
        cards=request.sample_cards
    )
    return result