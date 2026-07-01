from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ai_analyzer.llm.client import close_client, get_client
from ai_analyzer.pipeline import StructurePipeline
from ai_analyzer.schemas import AnalyzeStructureRequest
from ai_analyzer.services import LLMAnalysisError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: verify LLM connectivity on startup, close client on shutdown."""
    # Startup: verify the LLM client can reach Ollama
    client = get_client()
    try:
        await client.models.list()
    except Exception as exc:
        raise RuntimeError(
            f"LLM service (Ollama) is not reachable: {exc}"
        ) from exc
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
    """Liveness probe that also verifies LLM connectivity."""
    client = get_client()
    try:
        await client.models.list()
        return {"status": "healthy", "service": "ai_analyzer", "llm": "connected"}
    except Exception:
        return {"status": "degraded", "service": "ai_analyzer", "llm": "unreachable"}


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
                "message": "Invalid request format",
                "detail": {"validation_errors": exc.errors()},
            },
        },
    )


# Регистрируем глобальный обработчик ошибки
@app.exception_handler(LLMAnalysisError)
async def llm_analysis_error_handler(
    request: Request, exc: LLMAnalysisError
) -> JSONResponse:
    # Map specific error codes to HTTP status codes
    status_code: int
    if exc.code in ("INCOMPLETE_OUTPUT", "LLM_API_ERROR"):
        status_code = 504
    elif exc.code in ("INVALID_MODEL_OUTPUT", "INVALID_JSON", "EMPTY_RESPONSE", "EMPTY_CONTENT"):
        status_code = 422
    else:
        status_code = 500

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "error",
            "error": {
                "code": exc.code,
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
