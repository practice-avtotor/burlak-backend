"""Redis-backed job status cache with TTL.

Reduces SQLite read pressure during high-frequency polling.
Cache-aside pattern: write-through on mutations, read-through on queries.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.config import get_settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

JOB_CACHE_PREFIX = "job:status:"


def _cache_ttl() -> int:
    return get_settings().redis_cache_ttl


async def cache_job_status(job_id: int, data: dict[str, Any]) -> None:
    """Write job status to Redis cache (write-through)."""
    client = await get_redis()
    key = f"{JOB_CACHE_PREFIX}{job_id}"
    await client.set(key, json.dumps(data, default=str), ex=_cache_ttl())


async def get_cached_job_status(job_id: int) -> dict[str, Any] | None:
    """Read job status from Redis cache. Returns None on miss."""
    client = await get_redis()
    key = f"{JOB_CACHE_PREFIX}{job_id}"
    raw = await client.get(key)
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    result: dict[str, Any] = json.loads(raw)
    return result


async def invalidate_job_cache(job_id: int) -> None:
    """Invalidate cache for a specific job."""
    client = await get_redis()
    key = f"{JOB_CACHE_PREFIX}{job_id}"
    await client.delete(key)


async def invalidate_all_job_cache() -> None:
    """Clear all job caches (for admin operations)."""
    client = await get_redis()
    keys: list[str] = []
    async for key in client.scan_iter(f"{JOB_CACHE_PREFIX}*"):
        keys.append(str(key))
    if keys:
        await client.delete(*keys)
