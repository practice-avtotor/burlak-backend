"""Redis client management with connection pooling and health checks.

Provides both async (FastAPI) and sync (Celery worker) Redis clients
with automatic reconnection, health checks, and graceful shutdown.
"""

from __future__ import annotations

import logging
from typing import Any

import redis as sync_redis
import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# --- Async pool (FastAPI) ---
_async_pool: aioredis.ConnectionPool | None = None
_async_client: aioredis.Redis | None = None

# --- Sync pool (Celery workers) ---
_sync_pool: sync_redis.ConnectionPool | None = None
_sync_client: sync_redis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Get or create the async Redis client (FastAPI context).

    Uses a connection pool with configurable max connections,
    timeout, and automatic health checks.
    """
    global _async_pool, _async_client  # noqa: PLW0603
    if _async_client is not None:
        return _async_client

    settings = get_settings()
    _async_pool = aioredis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_timeout,
        socket_timeout=settings.redis_socket_timeout,
        retry_on_timeout=True,
        health_check_interval=settings.redis_health_check_interval,
    )
    _async_client = aioredis.Redis(connection_pool=_async_pool)
    logger.info("Async Redis pool created: %s", settings.redis_url)
    return _async_client


def get_sync_redis() -> sync_redis.Redis:
    """Get or create the sync Redis client (Celery worker context).

    Uses a separate connection pool from the async client.
    """
    global _sync_pool, _sync_client  # noqa: PLW0603
    if _sync_client is not None:
        return _sync_client

    settings = get_settings()
    _sync_pool = sync_redis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
        socket_connect_timeout=settings.redis_socket_timeout,
        socket_timeout=settings.redis_socket_timeout,
        retry_on_timeout=True,
        health_check_interval=settings.redis_health_check_interval,
    )
    _sync_client = sync_redis.Redis(connection_pool=_sync_pool)
    logger.info("Sync Redis pool created: %s", settings.redis_url)
    return _sync_client


async def close_redis() -> None:
    """Gracefully close all async Redis connections.

    Called during FastAPI shutdown via lifespan.
    """
    global _async_pool, _async_client  # noqa: PLW0603
    if _async_client is not None:
        await _async_client.aclose()
        _async_client = None
    if _async_pool is not None:
        await _async_pool.aclose()
        _async_pool = None
    logger.info("Async Redis connections closed")


def close_sync_redis() -> None:
    """Close sync Redis pool.

    Called during Celery worker shutdown.
    """
    global _sync_pool, _sync_client  # noqa: PLW0603
    if _sync_client is not None:
        _sync_client.close()
        _sync_client = None
    if _sync_pool is not None:
        _sync_pool.disconnect()
        _sync_pool = None
    logger.info("Sync Redis connections closed")


async def check_redis_health() -> dict[str, Any]:
    """Check Redis connectivity and return health status dict.

    Returns:
        Dict with 'redis' status and optional 'redis_version' or 'error'.
    """
    try:
        client = await get_redis()
        await client.ping()
        info = await client.info("server")
        return {
            "redis": "healthy",
            "redis_version": info.get("redis_version", "unknown"),
        }
    except Exception as exc:
        logger.warning("Redis health check failed: %s", exc)
        return {"redis": "unhealthy", "error": str(exc)}
