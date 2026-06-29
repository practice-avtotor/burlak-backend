"""Tests for Workstream D: Aggregation & Packaging.

Covers:
  1. ComparisonService unit tests (load_bom, load_cards_data, compare, generate_report).
  2. Package task unit tests (ZIP creation, cleanup, status).
  3. Integration tests for aggregate + package pipeline.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.sync_repository import (
    get_job_files,
    get_mapping_config,
    update_job_status,
)

# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_bom_path(tmp_path: Path) -> Path:
    """Create a minimal BOM.xlsx for testing."""
    import xlsxwriter

    path = tmp_path / "bom.xlsx"
    workbook = xlsxwriter.Workbook(str(path))
    ws = workbook.add_worksheet("BOM")

    # Header row
    ws.write(0, 0, "零件号")
    ws.write(0, 1, "零件名称")
    ws.write(0, 2, "English Name")
    ws.write(0, 3, "数量")

    # Data rows
    ws.write(1, 0, "P001")
    ws.write(1, 1, "螺栓M6")
    ws.write(1, 2, "Bolt M6")
    ws.write(1, 3, 10)

    ws.write(2, 0, "P002")
    ws.write(2, 1, "螺母M8")
    ws.write(2, 2, "Nut M8")
    ws.write(2, 3, 20)

    ws.write(3, 0, "P003")
    ws.write(3, 1, "垫圈")
    ws.write(3, 2, "Washer")
    ws.write(3, 3, 30)

    workbook.close()
    return path


@pytest.fixture
def sample_bom_with_config(tmp_path: Path) -> Path:
    """Create a BOM with mapping_config-style columns."""
    import xlsxwriter

    path = tmp_path / "bom_config.xlsx"
    workbook = xlsxwriter.Workbook(str(path))
    ws = workbook.add_worksheet("总装BOM")

    # Headers matching mapping_config
    headers = ["序号", "零件号", "零件名称", "零件名称(英文)", "用量"]
    for ci, h in enumerate(headers, 1):
        ws.write(0, ci - 1, h)

    ws.write(1, 0, 1)
    ws.write(1, 1, "P001")
    ws.write(1, 2, "螺栓M6")
    ws.write(1, 3, "Bolt M6")
    ws.write(1, 4, 10)

    ws.write(2, 0, 2)
    ws.write(2, 1, "P002")
    ws.write(2, 2, "螺母M8")
    ws.write(2, 3, "Nut M8")
    ws.write(2, 4, 20)

    workbook.close()
    return path


@pytest.fixture
def sample_mapping_config() -> dict:
    """Sample mapping_config for the BOM above."""
    return {
        "bom": {
            "sheets": [
                {
                    "sheet_name": "总装BOM",
                    "sheet_type": "bom_data",
                    "header_rows": [1],
                    "data_start_row": 2,
                    "columns": {
                        "part_no": {
                            "col_index": 2,
                            "header": "零件号",
                            "confidence": 0.98,
                        },
                        "name_cn": {
                            "col_index": 3,
                            "header": "零件名称",
                            "confidence": 0.95,
                        },
                        "name_en": {
                            "col_index": 4,
                            "header": "零件名称(英文)",
                            "confidence": 0.92,
                        },
                        "qty": {"col_index": 5, "header": "用量", "confidence": 0.97},
                    },
                }
            ]
        }
    }


@pytest.fixture
def job_dir_with_cards(tmp_path: Path) -> Path:
    """Create a job directory with sample card_*_materials.json files."""
    job_dir = tmp_path / "job_42"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Card 1
    card1 = {
        "card_number": "CARD-001",
        "card_path": "cards/CARD-001.xlsx",
        "parts": [
            {"part_no": "P001", "name_cn": "螺栓M6", "name_en": "Bolt M6", "qty": 5},
            {"part_no": "P002", "name_cn": "螺母M8", "name_en": "Nut M8", "qty": 10},
        ],
    }
    with open(job_dir / "card_1_materials.json", "w", encoding="utf-8") as f:
        json.dump(card1, f)

    # Card 2
    card2 = {
        "card_number": "CARD-002",
        "card_path": "cards/CARD-002.xlsx",
        "parts": [
            {"part_no": "P001", "name_cn": "螺栓M6", "name_en": "Bolt M6", "qty": 5},
            {"part_no": "P003", "name_cn": "垫圈", "name_en": "Washer", "qty": 15},
        ],
    }
    with open(job_dir / "card_2_materials.json", "w", encoding="utf-8") as f:
        json.dump(card2, f)

    return job_dir


@pytest.fixture
def job_dir_with_translated_cards(tmp_path: Path) -> Path:
    """Create a job directory with translated_cards/ subdirectory."""
    import xlsxwriter

    job_dir = tmp_path / "job_99"
    translated_dir = job_dir / "translated_cards"
    translated_dir.mkdir(parents=True, exist_ok=True)

    # Create a couple of translated XLSX files
    for fname in ["card_1_translated.xlsx", "card_2_translated.xlsx"]:
        path = translated_dir / fname
        wb = xlsxwriter.Workbook(str(path))
        ws = wb.add_worksheet()
        ws.write(0, 0, "test")
        wb.close()

    return job_dir


@pytest.fixture
def temp_db_with_job(temp_db_path: str) -> int:
    """Create a test job in the temp DB and return its ID."""
    conn = sqlite3.connect(temp_db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO jobs (status, stage, total, processed, failed,
                            bom_path, archive_path, bom_uploaded, archive_uploaded,
                            created_at, updated_at)
           VALUES ('processing', 'processing_cards', 2, 2, 0,
                   '/tmp/bom.xlsx', '/tmp/archive.zip', 1, 1,
                   '2024-01-01T00:00:00', '2024-01-01T00:00:00')"""
    )
    conn.commit()
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Add some cards
    conn.execute(
        "INSERT INTO cards (job_id, card_path, status, created_at, updated_at) VALUES (?, ?, 'success', '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (job_id, "cards/CARD-001.xlsx"),
    )
    conn.execute(
        "INSERT INTO cards (job_id, card_path, status, created_at, updated_at) VALUES (?, ?, 'success', '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (job_id, "cards/CARD-002.xlsx"),
    )
    conn.commit()
    conn.close()
    return job_id


@pytest.fixture
def temp_db_with_failed_cards(temp_db_path: str) -> int:
    """Create a test job with some failed cards."""
    conn = sqlite3.connect(temp_db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """INSERT INTO jobs (status, stage, total, processed, failed,
                            bom_path, archive_path, bom_uploaded, archive_uploaded,
                            created_at, updated_at)
           VALUES ('processing', 'processing_cards', 3, 2, 1,
                   '/tmp/bom.xlsx', '/tmp/archive.zip', 1, 1,
                   '2024-01-01T00:00:00', '2024-01-01T00:00:00')"""
    )
    conn.commit()
    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    conn.execute(
        "INSERT INTO cards (job_id, card_path, status, error_message, created_at, updated_at) VALUES (?, ?, 'success', NULL, '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (job_id, "cards/CARD-001.xlsx"),
    )
    conn.execute(
        "INSERT INTO cards (job_id, card_path, status, error_message, created_at, updated_at) VALUES (?, ?, 'failed', 'Parse error: invalid format', '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (job_id, "cards/CARD-002.xlsx"),
    )
    conn.execute(
        "INSERT INTO cards (job_id, card_path, status, error_message, created_at, updated_at) VALUES (?, ?, 'success', NULL, '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
        (job_id, "cards/CARD-003.xlsx"),
    )
    conn.commit()
    conn.close()
    return job_id


# ══════════════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════════════
# Package Task Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestPackage:
    """Unit tests for the package task logic."""

    def test_create_zip(self, job_dir_with_translated_cards: Path):
        """_create_zip should create a valid ZIP with all XLSX files."""
        from app.worker.tasks.package import _create_zip

        translated_dir = str(job_dir_with_translated_cards / "translated_cards")
        zip_path = str(job_dir_with_translated_cards / "translated_cards.zip")

        _create_zip(translated_dir, zip_path)

        assert os.path.exists(zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            assert len(names) == 2
            assert "card_1_translated.xlsx" in names
            assert "card_2_translated.xlsx" in names

    def test_create_zip_empty_dir(self, tmp_path: Path):
        """_create_zip should create an empty ZIP when directory is empty."""
        from app.worker.tasks.package import _create_zip

        empty_dir = str(tmp_path / "empty")
        os.makedirs(empty_dir, exist_ok=True)
        zip_path = str(tmp_path / "empty.zip")

        _create_zip(empty_dir, zip_path)
        assert os.path.exists(zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            assert len(zf.namelist()) == 0

    def test_create_zip_missing_dir(self, tmp_path: Path):
        """_create_zip should create empty ZIP when directory doesn't exist."""
        from app.worker.tasks.package import _create_zip

        missing_dir = str(tmp_path / "nonexistent")
        zip_path = str(tmp_path / "missing.zip")

        _create_zip(missing_dir, zip_path)
        assert os.path.exists(zip_path)

    def test_cleanup_translated_dir(self, job_dir_with_translated_cards: Path):
        """_cleanup_translated_dir should remove the directory."""
        from app.worker.tasks.package import _cleanup_translated_dir

        translated_dir = str(job_dir_with_translated_cards / "translated_cards")
        assert os.path.isdir(translated_dir)

        _cleanup_translated_dir(translated_dir)
        assert not os.path.exists(translated_dir)

    def test_cleanup_translated_dir_already_gone(self, tmp_path: Path):
        """_cleanup_translated_dir should not raise if dir already gone."""
        from app.worker.tasks.package import _cleanup_translated_dir

        gone_dir = str(tmp_path / "already_gone")
        _cleanup_translated_dir(gone_dir)  # should not raise

    def test_count_failed_cards(self, temp_db_path: str):
        """_count_failed_cards should return correct count."""
        from app.worker.tasks.package import _count_failed_cards

        # Create a job with failed cards
        conn = sqlite3.connect(temp_db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """INSERT INTO jobs (status, total, processed, failed, bom_uploaded, archive_uploaded, created_at, updated_at)
               VALUES ('processing', 2, 1, 1, 1, 1, '2024-01-01T00:00:00', '2024-01-01T00:00:00')"""
        )
        conn.commit()
        job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO cards (job_id, card_path, status, created_at, updated_at) VALUES (?, ?, 'failed', '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
            (job_id, "bad.xlsx"),
        )
        conn.execute(
            "INSERT INTO cards (job_id, card_path, status, created_at, updated_at) VALUES (?, ?, 'success', '2024-01-01T00:00:00', '2024-01-01T00:00:00')",
            (job_id, "good.xlsx"),
        )
        conn.commit()
        conn.close()

        count = _count_failed_cards(job_id)
        assert count == 1


# ══════════════════════════════════════════════════════════════════════════════
# Sync Repository Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestSyncRepository:
    """Tests for the new sync_repository methods."""

    def test_get_job_files(self, temp_db_with_job: int, temp_db_path: str):
        """get_job_files should return bom_path and archive_path."""
        # Override settings.db_url to point to temp DB
        settings = get_settings()
        original = settings.db_url
        settings.db_url = temp_db_path
        try:
            bom_path, archive_path = get_job_files(temp_db_with_job)
            assert bom_path == "/tmp/bom.xlsx"
            assert archive_path == "/tmp/archive.zip"
        finally:
            settings.db_url = original

    def test_get_job_files_not_found(self, temp_db_path: str):
        """get_job_files should raise for non-existent job."""
        settings = get_settings()
        original = settings.db_url
        settings.db_url = temp_db_path
        try:
            with pytest.raises(ValueError, match="not found"):
                get_job_files(99999)
        finally:
            settings.db_url = original

    def test_get_mapping_config_none(self, temp_db_with_job: int, temp_db_path: str):
        """get_mapping_config should return empty dict when not set."""
        settings = get_settings()
        original = settings.db_url
        settings.db_url = temp_db_path
        try:
            config = get_mapping_config(temp_db_with_job)
            assert config == {}
        finally:
            settings.db_url = original

    def test_get_mapping_config_with_value(
        self, temp_db_with_job: int, temp_db_path: str
    ):
        """get_mapping_config should return parsed JSON."""
        settings = get_settings()
        original = settings.db_url
        settings.db_url = temp_db_path
        try:
            # Set mapping config
            conn = sqlite3.connect(temp_db_path)
            conn.execute(
                "UPDATE jobs SET mapping_config = ? WHERE id = ?",
                ('{"bom": {"sheets": []}}', temp_db_with_job),
            )
            conn.commit()
            conn.close()

            config = get_mapping_config(temp_db_with_job)
            assert config == {"bom": {"sheets": []}}
        finally:
            settings.db_url = original

    def test_update_job_status(self, temp_db_with_job: int, temp_db_path: str):
        """update_job_status should update status and stage."""
        settings = get_settings()
        original = settings.db_url
        settings.db_url = temp_db_path
        try:
            update_job_status(temp_db_with_job, "done", "completed")

            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, stage FROM jobs WHERE id = ?", (temp_db_with_job,)
            ).fetchone()
            conn.close()

            assert row["status"] == "done"
            assert row["stage"] == "completed"
        finally:
            settings.db_url = original

    def test_get_failed_cards(self, temp_db_with_failed_cards: int, temp_db_path: str):
        """get_failed_cards should return all failed card paths and error messages."""
        from app.db.sync_repository import get_failed_cards

        settings = get_settings()
        original = settings.db_url
        settings.db_url = temp_db_path
        try:
            failed_cards = get_failed_cards(temp_db_with_failed_cards)
            assert len(failed_cards) == 1
            assert failed_cards[0]["card_path"] == "cards/CARD-002.xlsx"
            assert failed_cards[0]["error_message"] == "Parse error: invalid format"
        finally:
            settings.db_url = original



# ══════════════════════════════════════════════════════════════════════════════
# Integration Tests
# ══════════════════════════════════════════════════════════════════════════════


class TestAggregateIntegration:
    """Integration tests for aggregate + package pipeline."""

    def test_aggregate_happy_path(
        self,
        temp_db_with_job: int,
        temp_db_path: str,
        sample_bom_path: Path,
        job_dir_with_cards: Path,
        mock_storage_path: Path,
    ):
        """Full aggregate pipeline with valid data."""
        settings = get_settings()
        original_db = settings.db_url
        original_storage = settings.storage_path
        settings.db_url = temp_db_path
        settings.storage_path = str(mock_storage_path)

        try:
            # Set up: update bom_path to point to our test BOM
            conn = sqlite3.connect(temp_db_path)
            conn.execute(
                "UPDATE jobs SET bom_path = ? WHERE id = ?",
                (str(sample_bom_path), temp_db_with_job),
            )
            conn.commit()
            conn.close()

            # Copy card JSONs to the mock storage job dir
            import shutil

            job_dir = mock_storage_path / str(temp_db_with_job)
            job_dir.mkdir(parents=True, exist_ok=True)
            for f in job_dir_with_cards.iterdir():
                shutil.copy2(str(f), str(job_dir / f.name))

            # Run aggregate via Celery eager mode
            from app.worker.tasks.aggregate import aggregate as aggregate_task

            aggregate_task.delay(temp_db_with_job)

            # Verify diff.xlsx was created
            diff_path = job_dir / "diff.xlsx"
            assert diff_path.exists(), f"diff.xlsx not found at {diff_path}"

            # Verify job completed successfully (aggregate + package run in eager mode)
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, stage FROM jobs WHERE id = ?",
                (temp_db_with_job,),
            ).fetchone()
            conn.close()
            assert row["status"] == "done"
            assert row["stage"] == "completed"

        finally:
            settings.db_url = original_db
            settings.storage_path = original_storage

    def test_package_happy_path(
        self,
        temp_db_with_job: int,
        temp_db_path: str,
        job_dir_with_translated_cards: Path,
        mock_storage_path: Path,
    ):
        """Full package pipeline with valid data."""
        settings = get_settings()
        original_db = settings.db_url
        original_storage = settings.storage_path
        settings.db_url = temp_db_path
        settings.storage_path = str(mock_storage_path)

        try:
            # Copy translated cards to mock storage
            import shutil

            job_dir = mock_storage_path / str(temp_db_with_job)
            if job_dir.exists():
                shutil.rmtree(str(job_dir))
            shutil.copytree(
                str(job_dir_with_translated_cards),
                str(job_dir),
            )

            # Run package via Celery eager mode
            from app.worker.tasks.package import package as package_task

            package_task.delay(temp_db_with_job)

            # Verify ZIP was created
            zip_path = job_dir / "translated_cards.zip"
            assert zip_path.exists(), f"ZIP not found at {zip_path}"

            # Verify translated_cards/ was removed
            translated_dir = job_dir / "translated_cards"
            assert not translated_dir.exists(), "translated_cards/ should be removed"

            # Verify final status (no failed cards → done)
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, stage FROM jobs WHERE id = ?",
                (temp_db_with_job,),
            ).fetchone()
            conn.close()
            assert row["status"] == "done"
            assert row["stage"] == "completed"

        finally:
            settings.db_url = original_db
            settings.storage_path = original_storage

    def test_package_with_failed_cards(
        self,
        temp_db_with_failed_cards: int,
        temp_db_path: str,
        job_dir_with_translated_cards: Path,
        mock_storage_path: Path,
    ):
        """Package should set status=error when failed > 0."""
        settings = get_settings()
        original_db = settings.db_url
        original_storage = settings.storage_path
        settings.db_url = temp_db_path
        settings.storage_path = str(mock_storage_path)

        try:
            import shutil

            job_dir = mock_storage_path / str(temp_db_with_failed_cards)
            if job_dir.exists():
                shutil.rmtree(str(job_dir))
            shutil.copytree(
                str(job_dir_with_translated_cards),
                str(job_dir),
            )

            from app.worker.tasks.package import package as package_task

            package_task.delay(temp_db_with_failed_cards)

            # Verify final status is error
            conn = sqlite3.connect(temp_db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT status, stage FROM jobs WHERE id = ?",
                (temp_db_with_failed_cards,),
            ).fetchone()
            conn.close()
            assert row["status"] == "error"
            assert row["stage"] == "completed_with_errors"

        finally:
            settings.db_url = original_db
            settings.storage_path = original_storage
