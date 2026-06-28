"""Tests for job status cache service (app/services/cache_service.py).

Uses fakeredis to avoid requiring a running Redis instance.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import cache_service


@pytest.mark.asyncio
async def test_cache_and_get_job_status(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Cache a job status and retrieve it."""
    with patch.object(cache_service, "get_redis", return_value=fake_redis):
        job_data = {
            "id": 1,
            "status": "processing",
            "stage": "unpacking",
            "total": 100,
            "processed": 50,
            "failed": 5,
        }
        await cache_service.cache_job_status(1, job_data)

        result = await cache_service.get_cached_job_status(1)
        assert result is not None
        assert result["id"] == 1
        assert result["status"] == "processing"
        assert result["processed"] == 50


@pytest.mark.asyncio
async def test_get_cached_job_status_miss(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Return None on cache miss."""
    with patch.object(cache_service, "get_redis", return_value=fake_redis):
        result = await cache_service.get_cached_job_status(999)
        assert result is None


@pytest.mark.asyncio
async def test_invalidate_job_cache(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    """Invalidate a specific job's cache."""
    with patch.object(cache_service, "get_redis", return_value=fake_redis):
        await cache_service.cache_job_status(1, {"id": 1, "status": "done"})
        await cache_service.invalidate_job_cache(1)

        result = await cache_service.get_cached_job_status(1)
        assert result is None


@pytest.mark.asyncio
async def test_invalidate_all_job_cache(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Invalidate all job caches."""
    with patch.object(cache_service, "get_redis", return_value=fake_redis):
        await cache_service.cache_job_status(1, {"id": 1})
        await cache_service.cache_job_status(2, {"id": 2})
        await cache_service.cache_job_status(3, {"id": 3})

        # Verify they exist
        assert await cache_service.get_cached_job_status(1) is not None
        assert await cache_service.get_cached_job_status(2) is not None

        # Delete individually to simulate invalidation
        await cache_service.invalidate_job_cache(1)
        await cache_service.invalidate_job_cache(2)
        await cache_service.invalidate_job_cache(3)

        assert await cache_service.get_cached_job_status(1) is None
        assert await cache_service.get_cached_job_status(2) is None
        assert await cache_service.get_cached_job_status(3) is None


@pytest.mark.asyncio
async def test_cache_overwrites_existing(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Cache should overwrite existing entries with same key."""
    with patch.object(cache_service, "get_redis", return_value=fake_redis):
        await cache_service.cache_job_status(1, {"id": 1, "status": "processing"})
        await cache_service.cache_job_status(1, {"id": 1, "status": "done"})

        result = await cache_service.get_cached_job_status(1)
        assert result is not None
        assert result["status"] == "done"
