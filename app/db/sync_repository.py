import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings


@dataclass
class ProgressResult:
    is_complete: bool
    processed: int
    failed: int
    total: int


def increment_progress(
    job_id: int, card_path: str, *, success: bool, error_message: str | None = None
) -> ProgressResult:
    """Atomically and idempotently updates card status and increments progress counters in jobs.

    Uses BEGIN IMMEDIATE transaction on raw sqlite3 connection to prevent WAL deadlocks.
    """
    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]
    now = datetime.now(UTC).isoformat()

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")

        # 1. Check current status of the card to ensure idempotency
        cursor = conn.execute(
            "SELECT status FROM cards WHERE job_id = ? AND card_path = ?",
            (job_id, card_path),
        )
        card = cursor.fetchone()
        if not card:
            raise ValueError(f"Card {card_path} not found for job {job_id}")

        current_status = card["status"]
        new_status = "success" if success else "failed"

        # 2. Determine counter adjustments
        processed_delta = 0
        failed_delta = 0

        if current_status == "pending":
            if success:
                processed_delta = 1
            else:
                failed_delta = 1
        elif current_status == "success" and not success:
            processed_delta = -1
            failed_delta = 1
        elif current_status == "failed" and success:
            processed_delta = 1
            failed_delta = -1

        # 3. Update the card record
        conn.execute(
            """
            UPDATE cards
            SET status = ?, error_message = ?, updated_at = ?
            WHERE job_id = ? AND card_path = ?
            """,
            (new_status, error_message, now, job_id, card_path),
        )

        # 4. Update the job counters if there are adjustments
        if processed_delta != 0 or failed_delta != 0:
            conn.execute(
                """
                UPDATE jobs
                SET processed = processed + ?, failed = failed + ?, updated_at = ?
                WHERE id = ?
                """,
                (processed_delta, failed_delta, now, job_id),
            )

        # 5. Fetch current job state
        cursor = conn.execute(
            "SELECT processed, failed, total FROM jobs WHERE id = ?", (job_id,)
        )
        job = cursor.fetchone()
        if not job:
            raise ValueError(f"Job {job_id} not found")

        processed = job["processed"]
        failed = job["failed"]
        total = job["total"]

        is_complete = processed + failed == total

        conn.commit()
        return ProgressResult(
            is_complete=is_complete,
            processed=processed,
            failed=failed,
            total=total,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_job_files(job_id: int) -> tuple[str, str]:
    """Returns ``(bom_path, archive_path)`` for the given *job_id*.

    Uses a raw sqlite3 connection (same pattern as ``increment_progress``).
    """
    db_path = _resolve_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT bom_path, archive_path FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} not found")
        bom_path = row["bom_path"]
        archive_path = row["archive_path"]
        if not bom_path:
            raise ValueError(f"Job {job_id} has no bom_path set")
        if not archive_path:
            raise ValueError(f"Job {job_id} has no archive_path set")
        return (bom_path, archive_path)
    finally:
        conn.close()


def get_mapping_config(job_id: int) -> dict[str, Any] | None:
    """Returns the ``mapping_config`` JSON for the given *job_id*.

    Returns ``None`` if the job has no mapping config set.
    """
    db_path = _resolve_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT mapping_config FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError(f"Job {job_id} not found")
        raw = row["mapping_config"]
        if raw is None:
            return None
        if isinstance(raw, str):
            return json.loads(raw)
        return raw  # already a dict
    finally:
        conn.close()


def update_job_status(
    job_id: int,
    status: str,
    stage: str | None = None,
) -> None:
    """Updates the ``status`` and optionally ``stage`` of a job.

    Uses ``BEGIN IMMEDIATE`` for WAL-safety.
    """
    db_path = _resolve_db_path()
    now = datetime.now(UTC).isoformat()
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if stage is not None:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, stage = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, stage, now, job_id),
            )
        else:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, job_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _resolve_db_path() -> str:
    """Resolve the SQLite database path from settings."""
    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]
    return db_path
