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
        "app.worker.tasks.process_heuristic",
        "app.worker.tasks.cleanup",
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
    # Support very slow machines: up to 3 hours per task.
    # soft limit sends SoftTimeLimitExceeded (allows graceful cleanup)
    # hard limit force-kills the worker process.
    task_soft_time_limit=10500,   # 2 h 55 min
    task_time_limit=10800,        # 3 hours
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
        "visibility_timeout": 10800,
        "fanout_prefix": True,
        "fanout_patterns": True,
    },
    broker_connection_retry_on_startup=True,
    # --- Result Backend ---
    result_expires=10800,
    result_backend_transport_options={
        "retry_policy": {
            "timeout": 5.0,
        },
    },
    # --- Queue routing ---
    # Exact task names — Celery does NOT support glob patterns in task_routes.
    # Each task has an explicit `name=` in its decorator to guarantee deterministic routing.
    task_routes={
        "app.worker.tasks.unpack.unpack": {"queue": "unpack"},
        "app.worker.tasks.analyze_mapping.analyze_mapping": {"queue": "mapping"},
        "app.worker.tasks.process_card.process_card": {"queue": "cards"},
        "app.worker.tasks.aggregate.aggregate": {"queue": "aggregate"},
        "app.worker.tasks.package.package": {"queue": "aggregate"},
        "app.worker.tasks.process_heuristic.process_heuristic": {"queue": "heuristic"},
        "app.worker.tasks.cleanup.cleanup_old_jobs": {"queue": "cleanup"},
    },
    task_create_missing_queues=True,

    # --- Beat schedule (periodic tasks) ---
    beat_schedule={
        "cleanup-old-jobs": {
            "task": "app.worker.tasks.cleanup.cleanup_old_jobs",
            "schedule": 3600,  # Every hour (3600 seconds)
            "kwargs": {"max_age_hours": 24},
        },
    },
)


@worker_shutdown.connect  # type: ignore[untyped-decorator]
def _shutdown_worker(**kwargs: object) -> None:
    """Close sync Redis connections on worker shutdown."""
    from app.core.redis import close_sync_redis

    close_sync_redis()
