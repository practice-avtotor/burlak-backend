"""Tests for Redis client factory (app/core/redis.py).

Uses fakeredis to avoid requiring a running Redis instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from app.core import redis as redis_module


@pytest.mark.asyncio
async def test_get_redis_creates_client() -> None:
    """get_redis() should create and cache an async Redis client."""
    fake_server = fakeredis.aioredis.FakeServer()
    fake_client = fakeredis.aioredis.FakeRedis(server=fake_server)

    with patch.object(redis_module, "aioredis") as mock_aioredis:
        mock_pool = AsyncMock()
        mock_aioredis.ConnectionPool.from_url.return_value = mock_pool
        mock_aioredis.Redis.return_value = fake_client

        client = await redis_module.get_redis()
        assert client is not None
        mock_aioredis.ConnectionPool.from_url.assert_called_once()

        # Second call should reuse the cached client
        client2 = await redis_module.get_redis()
        assert client is client2
        assert mock_aioredis.ConnectionPool.from_url.call_count == 1


@pytest.mark.asyncio
async def test_close_redis_clears_globals() -> None:
    """close_redis() should clear the global client references."""
    fake_client = AsyncMock()
    fake_pool = AsyncMock()

    redis_module._async_client = fake_client
    redis_module._async_pool = fake_pool

    await redis_module.close_redis()

    assert redis_module._async_client is None
    assert redis_module._async_pool is None
    fake_client.aclose.assert_called_once()
    fake_pool.aclose.assert_called_once()


def test_close_sync_redis_clears_globals() -> None:
    """close_sync_redis() should clear the global client references."""
    fake_client = AsyncMock()
    fake_pool = AsyncMock()

    redis_module._sync_client = fake_client  # type: ignore[assignment]
    redis_module._sync_pool = fake_pool  # type: ignore[assignment]

    redis_module.close_sync_redis()

    assert redis_module._sync_client is None
    assert redis_module._sync_pool is None
    fake_client.close.assert_called_once()
    fake_pool.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_check_redis_health_healthy() -> None:
    """check_redis_health() returns healthy status when ping succeeds."""
    fake_client = AsyncMock()
    fake_client.ping = AsyncMock(return_value=True)
    fake_client.info = AsyncMock(return_value={"redis_version": "7.0.0"})

    with patch.object(redis_module, "get_redis", return_value=fake_client):
        result = await redis_module.check_redis_health()

    assert result["redis"] == "healthy"
    assert result["redis_version"] == "7.0.0"


@pytest.mark.asyncio
async def test_check_redis_health_unhealthy() -> None:
    """check_redis_health() returns unhealthy status when ping fails."""
    with patch.object(
        redis_module, "get_redis", side_effect=ConnectionError("Redis down")
    ):
        result = await redis_module.check_redis_health()

    assert result["redis"] == "unhealthy"
    assert "error" in result
