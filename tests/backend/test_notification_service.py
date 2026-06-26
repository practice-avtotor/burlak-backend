"""Tests for Pub/Sub notification service (app/services/notification_service.py).

Uses fakeredis to avoid requiring a running Redis instance.
Note: fakeredis has limited Pub/Sub support, so we test the
publish_progress function's payload construction and error handling.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services import notification_service


@pytest.mark.asyncio
async def test_publish_progress_constructs_correct_payload() -> None:
    """publish_progress should construct correct JSON payload."""
    mock_client = AsyncMock()
    mock_client.publish = AsyncMock(return_value=1)

    with patch.object(notification_service, "get_redis", return_value=mock_client):
        await notification_service.publish_progress(
            1, processed=50, failed=5, total=100, stage="processing_cards"
        )

        mock_client.publish.assert_called_once()
        call_args = mock_client.publish.call_args
        channel = call_args[0][0]
        payload = json.loads(call_args[0][1])

        assert channel == "job:1:progress"
        assert payload["job_id"] == 1
        assert payload["processed"] == 50
        assert payload["failed"] == 5
        assert payload["total"] == 100
        assert payload["stage"] == "processing_cards"
        assert payload["percent"] == 55.0


@pytest.mark.asyncio
async def test_publish_progress_calculates_percent() -> None:
    """Progress percent should be calculated correctly."""
    mock_client = AsyncMock()
    mock_client.publish = AsyncMock(return_value=1)

    with patch.object(notification_service, "get_redis", return_value=mock_client):
        await notification_service.publish_progress(
            2, processed=75, failed=25, total=100
        )

        payload = json.loads(mock_client.publish.call_args[0][1])
        assert payload["percent"] == 100.0


@pytest.mark.asyncio
async def test_publish_progress_zero_total() -> None:
    """Progress percent should be 0 when total is 0."""
    mock_client = AsyncMock()
    mock_client.publish = AsyncMock(return_value=1)

    with patch.object(notification_service, "get_redis", return_value=mock_client):
        await notification_service.publish_progress(3, processed=0, failed=0, total=0)

        payload = json.loads(mock_client.publish.call_args[0][1])
        assert payload["percent"] == 0


@pytest.mark.asyncio
async def test_publish_progress_graceful_on_error() -> None:
    """publish_progress should not raise on Redis errors (fire-and-forget)."""
    with patch.object(
        notification_service,
        "get_redis",
        side_effect=ConnectionError("Redis unavailable"),
    ):
        # Should not raise
        await notification_service.publish_progress(1, processed=0, failed=0, total=10)


@pytest.mark.asyncio
async def test_subscribe_progress_returns_pubsub() -> None:
    """subscribe_progress should return a PubSub object."""
    mock_client = AsyncMock()
    mock_pubsub = AsyncMock()
    mock_pubsub.subscribe = AsyncMock()
    # client.pubsub() is sync on redis.asyncio.Redis — use MagicMock-style
    mock_client.pubsub = lambda: mock_pubsub

    with patch.object(notification_service, "get_redis", return_value=mock_client):
        result = await notification_service.subscribe_progress(10)

        assert result is mock_pubsub
        mock_pubsub.subscribe.assert_called_once_with("job:10:progress")


@pytest.mark.asyncio
async def test_publish_progress_no_stage() -> None:
    """Progress payload should handle None stage."""
    mock_client = AsyncMock()
    mock_client.publish = AsyncMock(return_value=1)

    with patch.object(notification_service, "get_redis", return_value=mock_client):
        await notification_service.publish_progress(5, processed=10, failed=2, total=50)

        payload = json.loads(mock_client.publish.call_args[0][1])
        assert payload["stage"] is None
