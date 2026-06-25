"""Celery application configuration.

Broker: Redis (LPUSH/BRPOP) — Pull model for worker task distribution.
Backend: Redis — task results with TTL expiry.
Serialization: JSON only (no pickle for security).
"""

from __future__ import annotations

from celery import Celery  # type: ignore[import-untyped]
from celery.signals import worker_shutdown  # type: ignore[import-untyped]

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "burlak_worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "app.worker.tasks.unpack",
        "app.worker.tasks.analyze_mapping",
        "app.worker.tasks.process_card",
        "app.worker.tasks.aggregate",
        "app.worker.tasks.package",
    ],
)

celery_app.conf.update(
    # --- Serialization ---
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # --- Security ---
    task_always_eager=False,
    task_eager=False,
    # --- Timeouts ---
    task_soft_time_limit=300,
    task_time_limit=360,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # --- Retry ---
    task_default_retry_delay=10,
    task_max_retries=3,
    # --- Worker ---
    worker_prefetch_multiplier=4,
    worker_max_tasks_per_child=100,
    worker_cancel_long_running_tasks_on_connection_loss=True,
    # --- Broker ---
    broker_transport_options={
        "visibility_timeout": 3600,
        "fanout_prefix": True,
        "fanout_patterns": True,
    },
    broker_connection_retry_on_startup=True,
    # --- Result Backend ---
    result_expires=3600,
    result_backend_transport_options={
        "retry_policy": {
            "timeout": 5.0,
        },
    },
    # --- Queue routing ---
    task_routes={
        "app.worker.tasks.unpack.*": {"queue": "unpack"},
        "app.worker.tasks.analyze_mapping.*": {"queue": "mapping"},
        "app.worker.tasks.process_card.*": {"queue": "cards"},
        "app.worker.tasks.aggregate.*": {"queue": "aggregate"},
        "app.worker.tasks.package.*": {"queue": "aggregate"},
    },
    task_create_missing_queues=True,
)


@worker_shutdown.connect  # type: ignore[untyped-decorator]
def _shutdown_worker(**kwargs: object) -> None:
    """Close sync Redis connections on worker shutdown."""
    from app.core.redis import close_sync_redis

    close_sync_redis()
