import os

import aiosqlite
from fastapi import APIRouter, Depends, Response, status

from app.core.config import get_settings
from app.core.redis import check_redis_health
from app.db.database import get_async_db
from app.schemas.health import HealthChecks, HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check(
    response: Response, db: aiosqlite.Connection = Depends(get_async_db)
) -> HealthResponse:
    """System health check endpoint.

    Returns 200 OK with status information.
    Checks SQLite, Redis, and storage connectivity.
    """
    settings = get_settings()
    try:
        await db.execute("SELECT 1")
        db_status = "ok"
    except Exception as e:
        db_status = f"failed: {e}"
    try:
        redis_result = await check_redis_health()
        redis_status = (
            "ok"
            if redis_result["redis"] == "healthy"
            else f"failed: {redis_result.get('error', 'unknown')}"
        )
    except Exception as e:
        redis_status = f"failed: {e}"
    try:
        os.makedirs(settings.storage_path, exist_ok=True)
        temp_file_path = os.path.join(settings.storage_path, ".health_check_temp")
        with open(temp_file_path, "w") as f:
            f.write("healthcheck_ok")
        os.remove(temp_file_path)
        storage_status = "ok"
    except Exception as e:
        storage_status = f"failed: {e}"

    is_healthy = db_status == "ok" and redis_status == "ok" and storage_status == "ok"
    if not is_healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="healthy" if is_healthy else "unhealthy",
        checks=HealthChecks(
            database=db_status,
            redis=redis_status,
            storage=storage_status,
        ),
    )
