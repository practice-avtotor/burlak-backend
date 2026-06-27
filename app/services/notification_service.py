"""Redis Pub/Sub for real-time job progress notifications.

Frontend subscribes via SSE, backend publishes on each
increment_progress call. Channel: job:{job_id}:progress
"""

from __future__ import annotations

import json
import logging

import redis.asyncio as aioredis

from app.core.redis import get_redis

logger = logging.getLogger(__name__)

CHANNEL_PREFIX = "job:"


async def publish_progress(
    job_id: int,
    *,
    processed: int,
    failed: int,
    total: int,
    stage: str | None = None,
) -> None:
    """Publish progress event to Redis channel.

    Fire-and-forget: publication failure must not block the caller.
    """
    try:
        client = await get_redis()
        channel = f"{CHANNEL_PREFIX}{job_id}:progress"
        payload = json.dumps(
            {
                "job_id": job_id,
                "processed": processed,
                "failed": failed,
                "total": total,
                "stage": stage,
                "percent": round((processed + failed) / total * 100, 1)
                if total > 0
                else 0,
            }
        )
        await client.publish(channel, payload)
    except Exception as exc:
        logger.warning("Failed to publish progress for job %d: %s", job_id, exc)


async def subscribe_progress(job_id: int) -> aioredis.client.PubSub:
    """Return pubsub object subscribed to a job's progress channel.

    Caller must iterate with:
        pubsub = await subscribe_progress(job_id)
        async for message in pubsub.listen():
            ...
        await pubsub.unsubscribe()
        await pubsub.aclose()
    """
    client = await get_redis()
    pubsub = client.pubsub()
    channel = f"{CHANNEL_PREFIX}{job_id}:progress"
    await pubsub.subscribe(channel)
    return pubsub
