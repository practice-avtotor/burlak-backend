import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from celery.exceptions import Retry  # type: ignore[import-untyped]
from sqlalchemy import create_engine

from app.core.config import get_settings
from app.db.database import Base
from app.worker.tasks.process_card import process_card


@pytest.fixture(scope="function")
def temp_db_path(tmp_path: Path) -> Generator[str, None, None]:
    """Creates a temporary test SQLite database and configures settings."""
    db_file = tmp_path / "test.db"
    db_path = str(db_file)

    # Create connection and set WAL mode
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    conn.close()

    # Re-initialize engine and tables on the test database
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    engine.dispose()

    settings = get_settings()
    original_db_url = settings.db_url
    settings.db_url = db_path

    yield db_path

    settings.db_url = original_db_url


def test_process_card_task_no_raise_on_exhausted_retries(
    temp_db_path: str, tmp_path: Path
) -> None:
    """Verifies that process_card does not raise an exception when retries are exhausted."""
    # Setup settings
    settings = get_settings()
    original_db = settings.db_url
    settings.db_url = temp_db_path

    # 1. Create a job and a pending card in DB
    job_id = 123
    card_path = "test_card.xlsx"
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(temp_db_path)
    conn.execute(
        """
        INSERT INTO jobs (id, session_token, status, stage, bom_path, archive_path, processed, failed, total, bom_uploaded, archive_uploaded, created_at, updated_at)
        VALUES (?, ?, 'processing', 'parsing', 'bom.xlsx', 'archive.zip', 0, 0, 1, 1, 1, ?, ?)
        """,
        (job_id, "test-token-process-card-1", now, now),
    )
    conn.execute(
        """
        INSERT INTO cards (job_id, card_path, status, created_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?)
        """,
        (job_id, card_path, now, now),
    )
    conn.commit()
    conn.close()

    try:
        # Mock CardProcessingService to raise an error
        with (
            patch(
                "app.worker.tasks.process_card.CardProcessingService"
            ) as mock_service_cls,
            patch("app.worker.tasks.aggregate.aggregate.delay") as mock_aggregate_delay,
        ):
            mock_service = MagicMock()
            mock_service.process_card.side_effect = Exception(
                "Simulated ML or openpyxl error"
            )
            mock_service_cls.return_value = mock_service

            # Set up the bound Celery task mock
            # We simulate retries are exhausted (retries = max_retries = 3)
            mock_task = MagicMock()
            mock_task.request = MagicMock()
            mock_task.request.retries = 3
            mock_task.max_retries = 3

            # Run the task directly via the underlying function __func__
            process_card.run.__func__(mock_task, job_id, card_path)

            # Assertions:
            # 1. CardProcessingService.process_card was called
            mock_service.process_card.assert_called_once_with(card_path)
            # 2. CardProcessingService.write_error_file was called
            mock_service.write_error_file.assert_called_once()
            # 3. aggregate.delay was called
            mock_aggregate_delay.assert_called_once_with(job_id)

            # 4. Verify SQLite DB was updated: processed=0, failed=1
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            card = conn.execute(
                "SELECT * FROM cards WHERE job_id = ? AND card_path = ?",
                (job_id, card_path),
            ).fetchone()
            conn.close()

            assert card["status"] == "failed"
            assert job["failed"] == 1
            assert job["processed"] == 0

    finally:
        settings.db_url = original_db


def test_process_card_task_retries_if_remaining(temp_db_path: str) -> None:
    """Verifies that process_card raises Retry when retries are remaining."""
    settings = get_settings()
    original_db = settings.db_url
    settings.db_url = temp_db_path

    job_id = 456
    card_path = "test_card_retry.xlsx"
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(temp_db_path)
    conn.execute(
        """
        INSERT INTO jobs (id, session_token, status, stage, bom_path, archive_path, processed, failed, total, bom_uploaded, archive_uploaded, created_at, updated_at)
        VALUES (?, ?, 'processing', 'parsing', 'bom.xlsx', 'archive.zip', 0, 0, 1, 1, 1, ?, ?)
        """,
        (job_id, "test-token-process-card-2", now, now),
    )
    conn.execute(
        """
        INSERT INTO cards (job_id, card_path, status, created_at, updated_at)
        VALUES (?, ?, 'pending', ?, ?)
        """,
        (job_id, card_path, now, now),
    )
    conn.commit()
    conn.close()

    try:
        with patch(
            "app.worker.tasks.process_card.CardProcessingService"
        ) as mock_service_cls:
            mock_service = MagicMock()
            mock_service.process_card.side_effect = Exception(
                "Simulated ML or openpyxl error"
            )
            mock_service_cls.return_value = mock_service

            mock_task = MagicMock()
            mock_task.request = MagicMock()
            mock_task.request.retries = 1
            mock_task.max_retries = 3
            # self.retry raises Retry
            mock_task.retry.side_effect = Retry("Celery retry")

            with pytest.raises(Retry):
                process_card.run.__func__(mock_task, job_id, card_path)

            mock_task.retry.assert_called_once()

            # DB should NOT be marked as failed yet because we are retrying
            conn = sqlite3.connect(temp_db_path)
            card = conn.execute(
                "SELECT status FROM cards WHERE job_id = ?", (job_id,)
            ).fetchone()
            conn.close()
            assert card[0] == "pending"

    finally:
        settings.db_url = original_db
