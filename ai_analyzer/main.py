from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ai_analyzer.llm.client import close_client, get_client
from ai_analyzer.pipeline import StructurePipeline
from ai_analyzer.schemas import AnalyzeStructureRequest
from ai_analyzer.services import LLMAnalysisError


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: verify LLM connectivity on startup, close client on shutdown."""
    # Startup: verify the LLM client can be created
    try:
        get_client()
    except Exception as exc:
        raise RuntimeError(f"Failed to initialise LLM client: {exc}") from exc
    yield
    # Shutdown: close the HTTP client session gracefully
    await close_client()


app = FastAPI(title="ML Structure Analysis Service", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get("/health")
@app.get("/api/v1/health")
async def health() -> dict[str, object]:
    """Liveness probe for container orchestration."""
    return {"status": "healthy", "service": "ai_analyzer"}


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


# Перехват ошибок валидации FastAPI
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "error": {
                "code": "INVALID_INPUT",
                "message": "Неверный формат запроса",
                "detail": {"validation_errors": exc.errors()},
            },
        },
    )


# Регистрируем глобальный обработчик ошибки
@app.exception_handler(LLMAnalysisError)
async def llm_analysis_error_handler(
    request: Request, exc: LLMAnalysisError
) -> JSONResponse:
    contract_code = (
        "TIMEOUT" if "timeout" in str(exc.details).lower() else "ANALYSIS_FAILED"
    )
    status_code = 504 if contract_code == "TIMEOUT" else 500

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "error": {
                "code": contract_code,
                "message": exc.message,
                "detail": exc.details,
            },
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


# Валидация JSON в AnalyzeStructureRequest происходит автоматически
@app.post("/api/v1/analyze-structure")
async def analyze_structure(request: AnalyzeStructureRequest) -> dict[str, object]:
    pipeline = StructurePipeline()
    result = await pipeline.run(
        bom=request.bom,
        sample_cards=request.sample_cards,
        options=request.options,
    )
    return result
