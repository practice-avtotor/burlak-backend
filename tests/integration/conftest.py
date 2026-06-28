"""Integration test configuration.

Mocks Redis connections so integration tests don't need a running Redis instance.
"""

from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest


@pytest.fixture(autouse=True)
def _mock_redis_for_integration():
    """Mock Redis connections so integration tests don't need a running Redis.

    Patches async get_redis, close_redis, and cache/notification services
    that depend on Redis. Scoped to integration tests only to avoid
    interfering with backend tests that manage their own Redis mocks.
    """
    import fakeredis
    fake_client = fakeredis.aioredis.FakeRedis()
    fake_sync_client = fakeredis.FakeRedis()
    mock_pool = AsyncMock()

    patches = [
        patch("app.core.redis._async_client", fake_client),
        patch("app.core.redis._async_pool", mock_pool),
        patch("app.core.redis.get_redis", return_value=fake_client),
        patch("app.core.redis.close_redis", new_callable=AsyncMock),
        patch("app.core.redis._sync_client", fake_sync_client),
        patch("app.core.redis.get_sync_redis", return_value=fake_sync_client),
        patch("app.core.redis.close_sync_redis"),
        patch(
            "app.services.cache_service.get_redis", return_value=fake_client
        ),
        patch(
            "app.services.notification_service.get_redis", return_value=fake_client
        ),
        patch(
            "app.core.redis.check_redis_health",
            return_value={"redis": "healthy", "redis_version": "7.0.0"},
        ),
    ]

    for p in patches:
        p.start()

    yield

    for p in patches:
        p.stop()

    # Reset redis module globals
    from app.core import redis as redis_module

    redis_module._async_pool = None
    redis_module._async_client = None
    redis_module._sync_pool = None
    redis_module._sync_client = None
