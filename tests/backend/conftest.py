"""Shared fixtures for backend integration tests.

Provides fakeredis instances so tests can run without a live Redis server.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.core import redis as redis_module


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """Create a standalone fakeredis instance for direct cache/notification tests."""
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture(autouse=True)
def _reset_redis_globals() -> None:
    """Reset module-level Redis client/pool singletons before every backend test.

    Prevents state leaking between tests that mutate ``redis_module`` globals.
    """
    redis_module._async_pool = None
    redis_module._async_client = None
    redis_module._sync_pool = None
    redis_module._sync_client = None
    yield  # type: ignore[misc]
    redis_module._async_pool = None
    redis_module._async_client = None
    redis_module._sync_pool = None
    redis_module._sync_client = None
