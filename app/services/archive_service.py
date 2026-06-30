"""Archive service: ZIP packaging and streaming operations.

Delegates all file-level archive work so that Celery tasks
remain pure orchestrators with no inline business logic.

Note: ``create_translated_zip()`` was removed because the ``package`` task
(``app/worker/tasks/package.py``) handles ZIP assembly directly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
