"""pytest configuration for worker tests.

Enables Celery task eager mode so that ``.delay()`` calls execute
synchronously during tests, avoiding the need for a running Celery worker.
"""

from __future__ import annotations

import pytest

from app.worker.celery_app import celery_app


@pytest.fixture(autouse=True, scope="function")
def _celery_eager_mode() -> None:
    """Run Celery tasks synchronously in tests via ``task_always_eager``."""
    celery_app.conf.update(
        task_always_eager=True,
        task_eager_propagates=True,
    )
    yield
    celery_app.conf.update(
        task_always_eager=False,
        task_eager_propagates=False,
    )