from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from schemas import AnalyzeStructureRequest
from pipeline import StructurePipeline
from services import LLMAnalysisError
from fastapi.exceptions import RequestValidationError

app = FastAPI(title="ML Structure Analysis Service")

# Перехват ошибок валидации FastAPI
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "error": {
                "code": "INVALID_INPUT",
                "message": "Неверный формат запроса",
                "detail": {"validation_errors": exc.errors()}
            }
        }
    )

# Регистрируем глобальный обработчик ошибки
@app.exception_handler(LLMAnalysisError)
async def llm_analysis_error_handler(request: Request, exc: LLMAnalysisError):
    contract_code = "TIMEOUT" if "timeout" in str(exc.details).lower() else "ANALYSIS_FAILED"
    status_code = 504 if contract_code == "TIMEOUT" else 500

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "error": {
                "code": contract_code,
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
        bom=request.bom,
        sample_cards=request.sample_cards,
        options=request.options  # <-- добавить
    )
    return result