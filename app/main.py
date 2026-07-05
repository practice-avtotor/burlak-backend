import asyncio
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.exceptions import BurlakError
from app.schemas.job import ErrorDetail, ErrorResponse


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown events."""
    from app.core.redis import close_redis, get_redis
    from app.db.database import init_db

    # Startup: initialize database tables (non-blocking)
    await asyncio.to_thread(init_db)

    # Startup: initialize Redis connection pool
    await get_redis()
    yield
    # Shutdown: close Redis connections
    await close_redis()


app = FastAPI(
    title="BOM Verification System API",
    description="Backend API for BOM verification and card translation",
    version="0.1.0",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
    lifespan=lifespan,
)


# Paths exempt from API-key checks (health checks, docs, openapi schema)
_AUTH_EXEMPT_PATHS = frozenset({
    "/api/v1/health",
    "/api/v1/config",
    "/api/v1/docs",
    "/api/v1/redoc",
    "/api/v1/openapi.json",
    "/docs",
})


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Validate ``X-API-Key`` header on all requests.

    If ``settings.api_key`` is empty, the middleware is a no-op —
    useful for local development without auth.
    Exempt paths (health, docs, openapi) are always allowed through.
    """

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        key = settings.api_key
        # No key configured → auth disabled
        if not key:
            return await call_next(request)
        # Exempt paths always pass
        if request.url.path in _AUTH_EXEMPT_PATHS:
            return await call_next(request)
        provided = request.headers.get("X-API-Key", "")
        if secrets.compare_digest(provided, key):
            return await call_next(request)
        return JSONResponse(
            status_code=401,
            content=ErrorResponse(
                error=ErrorDetail(
                    code="UNAUTHORIZED",
                    message="Invalid or missing API key",
                    detail=None,
                )
            ).model_dump(),
        )


app.add_middleware(ApiKeyMiddleware)


@app.exception_handler(BurlakError)
async def burlak_error_handler(request: Request, exc: BurlakError) -> JSONResponse:
    """Global exception handler for all BurlakError subclasses.

    Returns uniform error response format:
    { "error": { "code": "...", "message": "...", "detail": null } }
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(
                code=exc.code,
                message=str(exc),
                detail=None,
            )
        ).model_dump(),
    )


@app.get("/docs", include_in_schema=False)
async def docs_redirect() -> RedirectResponse:
    """Redirect /docs to /api/v1/docs for convenience."""
    return RedirectResponse(url="/api/v1/docs")


# Register all v1 routers
# Public config endpoint — returns the API key so the frontend can send it.
# This endpoint is exempt from API key auth (see nginx.conf and _AUTH_EXEMPT_PATHS).
@app.get("/api/v1/config")
async def get_config() -> dict:
    """Return runtime configuration for the frontend.

    Includes the API key so the client can include it in subsequent requests.
    When ``api_key`` is empty, auth is disabled and the client won't send a key.
    """
    return {"api_key": get_settings().api_key}


app.include_router(v1_router)
