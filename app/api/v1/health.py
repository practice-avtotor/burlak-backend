from collections.abc import AsyncGenerator
from pathlib import Path

import aiosqlite
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.redis import check_redis_health
from app.db.database import get_async_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> JSONResponse:
    """System health check endpoint.

    Returns 200 OK with nested checks format when all healthy.
    Returns 503 Service Unavailable when any check fails.
    """
    settings = get_settings()
    checks: dict[str, str] = {}

    # Redis check
    redis_health = await check_redis_health()
    if redis_health.get("redis") == "healthy":
        checks["redis"] = "ok"
    else:
        checks["redis"] = f"failed: {redis_health.get('error', 'unknown')}"

    # SQLite check
    try:
        db_gen: AsyncGenerator[aiosqlite.Connection, None] = get_async_db()
        db = await db_gen.__anext__()
        try:
            await db.execute("SELECT 1")
        finally:
            try:
                await db_gen.__anext__()
            except StopAsyncIteration:
                pass
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"failed: {exc}"

    # Storage check
    storage_path = Path(settings.storage_path)
    if storage_path.exists() and storage_path.is_dir():
        checks["storage"] = "ok"
    else:
        checks["storage"] = "failed: path not accessible"

    # Determine overall status
    all_ok = all(v == "ok" for v in checks.values())
    status = "healthy" if all_ok else "unhealthy"
    status_code = 200 if all_ok else 503

    return JSONResponse(
        status_code=status_code,
        content={"status": status, "checks": checks},
    )
