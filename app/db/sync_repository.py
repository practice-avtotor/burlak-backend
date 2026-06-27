"""Synchronous SQLite repository for Celery workers.

Uses stdlib sqlite3 with BEGIN IMMEDIATE transactions for WAL safety.
Never use aiosqlite from Celery tasks — it forces asyncio.run() per task.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ProgressResult:
    is_complete: bool
    processed: int
    failed: int
    total: int


def _get_conn() -> sqlite3.Connection:
    """Create a sqlite3 connection with Row factory and WAL mode."""
    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _publish_progress(job_id: int, processed: int, failed: int, total: int) -> None:
    """Publish progress to Redis Pub/Sub (fire-and-forget)."""
    try:
        from app.core.redis import get_sync_redis

        client = get_sync_redis()
        channel = f"job:{job_id}:progress"
        payload = json.dumps(
            {
                "job_id": job_id,
                "processed": processed,
                "failed": failed,
                "total": total,
                "percent": round((processed + failed) / total * 100, 1)
                if total > 0
                else 0,
            }
        )
        client.publish(channel, payload)
    except Exception as exc:
        logger.debug("Redis publish skipped for job %d: %s", job_id, exc)


def increment_progress(
    job_id: int, card_path: str, *, success: bool, error_message: str | None = None
) -> ProgressResult:
    """Atomically and idempotently updates card status and increments progress counters.

    Uses BEGIN IMMEDIATE transaction on raw sqlite3 connection to prevent WAL deadlocks.
    Publishes progress to Redis Pub/Sub after commit.
    """
    now = datetime.now(UTC).isoformat()
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.execute(
            "SELECT status FROM cards WHERE job_id = ? AND card_path = ?",
            (job_id, card_path),
        )
        card = cursor.fetchone()
        if not card:
            raise ValueError(f"Card {card_path} not found for job {job_id}")

        current_status = card["status"]
        new_status = "success" if success else "failed"

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

        conn.execute(
            """
            UPDATE cards
            SET status = ?, error_message = ?, updated_at = ?
            WHERE job_id = ? AND card_path = ?
            """,
            (new_status, error_message, now, job_id, card_path),
        )

        if processed_delta != 0 or failed_delta != 0:
            conn.execute(
                """
                UPDATE jobs
                SET processed = processed + ?, failed = failed + ?, updated_at = ?
                WHERE id = ?
                """,
                (processed_delta, failed_delta, now, job_id),
            )

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

        _publish_progress(job_id, processed, failed, total)

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


def get_job(job_id: int) -> dict[str, Any] | None:
    """Retrieve a job by ID. Returns dict or None."""
    conn = _get_conn()
    try:
        cursor = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        if row is None:
            return None
        data = dict(row)
        if data.get("mapping_config"):
            data["mapping_config"] = json.loads(data["mapping_config"])
        return data
    finally:
        conn.close()


def create_cards_bulk(job_id: int, card_paths: list[str]) -> None:
    """Bulk-insert card records and set total count on job."""
    if not card_paths:
        return
    now = datetime.now(UTC).isoformat()
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cards_data = [(job_id, path, "pending", now, now) for path in card_paths]
        conn.executemany(
            """
            INSERT INTO cards (job_id, card_path, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            cards_data,
        )
        conn.execute(
            "UPDATE jobs SET total = ?, updated_at = ? WHERE id = ?",
            (len(card_paths), now, job_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _update_job(job_id: int, **fields: Any) -> None:
    """Update arbitrary fields on a job. Adds updated_at automatically."""
    now = datetime.now(UTC).isoformat()
    conn = _get_conn()
    try:
        set_clauses = [f"{k} = ?" for k in fields]
        set_clauses.append("updated_at = ?")
        values = list(fields.values()) + [now, job_id]
        conn.execute(
            f"UPDATE jobs SET {', '.join(set_clauses)} WHERE id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def update_job_stage(job_id: int, stage: str) -> None:
    """Update the processing stage of a job."""
    _update_job(job_id, stage=stage)


def update_mapping_config(job_id: int, mapping_config: dict[str, Any]) -> None:
    """Updates the mapping config field for a job synchronously (WAL-safe)."""
    _update_job(job_id, mapping_config=json.dumps(mapping_config))


def update_job_status(job_id: int, status: str, stage: str | None = None) -> None:
    """Updates the status and stage of a job synchronously (WAL-safe)."""
    _update_job(job_id, status=status, stage=stage)


def get_mapping_config(job_id: int) -> dict[str, Any]:
    """Retrieves the mapping config for a job synchronously."""
    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT mapping_config FROM jobs WHERE id = ?", (job_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Job {job_id} not found")
        config_str = row["mapping_config"]
        if not config_str:
            return {}
        return json.loads(config_str)  # type: ignore[no-any-return]
    finally:
        conn.close()


def get_job_files(job_id: int) -> tuple[str | None, str | None]:
    """Retrieves the absolute paths of BOM and archive for a job."""
    db_path = get_settings().db_url
    if db_path.startswith("sqlite:///"):
        db_path = db_path[len("sqlite:///") :]

    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            "SELECT bom_path, archive_path FROM jobs WHERE id = ?", (job_id,)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Job {job_id} not found")
        return row["bom_path"], row["archive_path"]
    finally:
        conn.close()


def create_cards(job_id: int, card_paths: list[str]) -> None:
    """Creates card records and sets the total card count on the job synchronously (WAL-safe)."""
    create_cards_bulk(job_id, card_paths)


def get_card_paths(job_id: int) -> list[str]:
    """Retrieves all card paths for a job synchronously."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "SELECT card_path FROM cards WHERE job_id = ? ORDER BY id",
            (job_id,),
        )
        return [row["card_path"] for row in cursor.fetchall()]
    finally:
        conn.close()


def set_job_status(job_id: int, status: str) -> None:
    """Set the final status of a job ('done' or 'error')."""
    _update_job(job_id, status=status)


def set_job_error(job_id: int, error_message: str) -> None:
    """Set job status to 'error'. Preserves current stage for debugging."""
    _update_job(job_id, status="error")
