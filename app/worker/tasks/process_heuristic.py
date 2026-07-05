"""Celery task: process_heuristic — full heuristic parser pipeline.

Replaces the multi-step ML pipeline (unpack → analyze_mapping → process_card → aggregate → package)
with a single monolithic task that uses the self-contained ``burlak_parser`` library.

The heuristic parser:
  1. Unpacks the ZIP archive to a temp directory
  2. Parses BOM using ``burlak_parser.bom_parser.parse_bom()`` (heuristic, no ML config)
  3. Parses cards using ``burlak_parser.card_parser.parse_cards()`` (built-in file classification)
  4. Splits multi-sheet cards (``split_cards_to_files`` → ``translated_cards.zip``)
  5. Compares ALL configurations using ``burlak_parser.comparator.compare_all_configs()``
  6. Generates ``diff.xlsx`` report via ``burlak_parser.report_generator.generate_discrepancy_report()``
  7. Updates DB status to ``'done'``

Output paths (same as ML pipeline):
  - ``{storage_path}/{job_id}/diff.xlsx``
  - ``{storage_path}/{job_id}/translated_cards.zip``
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import tempfile

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


def _allow_child_processes() -> None:
    """Allow ProcessPoolExecutor to spawn child processes from Celery workers.

    Celery marks worker processes as daemonic, which prevents stdlib
    multiprocessing from creating child processes (raises
    "daemonic processes are not allowed to have children").
    This patches the flag to False so ProcessPoolExecutor works normally.
    """
    try:
        proc = multiprocessing.current_process()
        if proc.daemon:
            proc.daemon = False
            logger.info("Patched multiprocessing daemon flag: allowed child processes")
    except Exception as e:
        logger.warning("Could not patch daemon flag: %s", e)
settings = get_settings()


@celery_app.task(
    bind=True,
    max_retries=2,
    retry_backoff=True,
    retry_backoff_max=60,
    acks_late=True,
)
def process_heuristic(self: Task, job_id: int) -> None:
    """Run the full heuristic parser pipeline for a single job.

    This is a monolithic task that performs all processing steps
    sequentially.  It uses ``burlak_parser`` — a self-contained
    heuristic parser that does NOT require an ML service.

    Args:
        job_id: The job ID in the database.

    Raises:
        self.retry: On transient errors (up to max_retries).
    """
    logger.info("Starting heuristic processing for job %d", job_id)

    # Fix "daemonic processes are not allowed to have children" in Celery workers
    _allow_child_processes()

    # ── Temp dirs ────────────────────────────────────────────────
    # Extracted card files live in a temp directory; final artifacts
    # go to the standard storage path.
    tmp_dir: str | None = None
    try:
        # ── Step 0: Update status ────────────────────────────────
        sync_repository.update_job_status(job_id, "processing", stage="extracting_cards")

        # ── Step 1: Get file paths ───────────────────────────────
        bom_path, archive_path = sync_repository.get_job_files(job_id)
        if not bom_path or not os.path.isfile(bom_path):
            raise FileNotFoundError(f"BOM file not found for job {job_id}: {bom_path}")
        if not archive_path or not os.path.isfile(archive_path):
            raise FileNotFoundError(f"Archive file not found for job {job_id}: {archive_path}")

        logger.info("Job %d: BOM=%s, Archive=%s", job_id, bom_path, archive_path)

        # ── Step 2: Extract ZIP to temp directory ───────────────
        tmp_dir = tempfile.mkdtemp(prefix=f"burlak_heuristic_{job_id}_")
        sync_repository.update_job_status(job_id, "processing", stage="unpacking")

        _extract_archive(archive_path, tmp_dir)

        # Count extracted Excel files early so frontend can show real progress.
        # Only count .xlsx/.xls (not .zip) since nested ZIPs are extracted
        # recursively by parse_cards() and will be counted by _card_progress.
        excel_count = 0
        for root, _dirs, filenames in os.walk(tmp_dir):
            for fn in filenames:
                if fn.lower().endswith((".xlsx", ".xls")):
                    excel_count += 1
        logger.info("Job %d: found %d Excel files in archive", job_id, excel_count)
        if excel_count > 0:
            sync_repository.update_job_status(
                job_id, "processing", stage="parsing_bom",
                total=excel_count, processed=0, failed=0,
            )

        # ── Step 3: Parse BOM (fully heuristic) ─────────────────
        sync_repository.update_job_status(job_id, "processing", stage="parsing_bom")
        logger.info("Job %d: parsing BOM heuristically...", job_id)

        from burlak_parser.bom_parser import BOMService

        bom_service = BOMService()
        bom = bom_service.load(bom_path)

        logger.info(
            "Job %d: BOM parsed: %d parts, %d configs",
            job_id, len(bom.parts), len(bom.config_names),
        )

        # ── Step 4: Parse cards (heuristic, with classification) ─
        sync_repository.update_job_status(job_id, "processing", stage="parsing_cards")
        logger.info("Job %d: parsing cards heuristically...", job_id)

        from burlak_parser.card_parser import CardService

        # _allow_child_processes() was called at the top of this task
        # to patch the daemon flag so ProcessPoolExecutor works.
        card_service = CardService()

        def _card_progress(processed: int, total: int, filename: str) -> None:
            """Update job progress after each card file is parsed."""
            sync_repository.update_job_status(
                job_id, "processing",
                stage=f"parsing_cards:{filename}",
                total=total, processed=processed, failed=0,
            )

        cards = card_service.load(tmp_dir, on_progress=_card_progress)

        logger.info(
            "Job %d: cards parsed: %d cards, %d unique parts, %d service files skipped",
            job_id,
            cards.total_cards_processed,
            len(cards.all_parts),
            cards.service_files_skipped,
        )

        # ── Step 5: Split multi-sheet cards ─────────────────────
        sync_repository.update_job_status(job_id, "processing", stage="splitting_cards")
        logger.info("Job %d: splitting multi-sheet cards...", job_id)

        split_dir = os.path.join(tmp_dir, "_split_cards")
        from burlak_parser.card_parser import split_cards_to_files

        def _split_progress(processed: int, total: int, filename: str) -> None:
            """Update job progress after each card file is split."""
            sync_repository.update_job_status(
                job_id, "processing",
                stage=f"splitting_cards:{filename}",
                total=total, processed=processed, failed=0,
            )

        created_files = split_cards_to_files(
            cards, split_dir, on_progress=_split_progress,
        )
        logger.info("Job %d: created %d split card files", job_id, len(created_files))

        # ── Step 5b: Create split cards ZIP ─────────────────────
        job_dir = os.path.join(settings.storage_path, str(job_id))
        os.makedirs(job_dir, exist_ok=True)
        split_zip_path = os.path.join(job_dir, "translated_cards.zip")

        from burlak_parser.report_generator import create_split_cards_archive

        create_split_cards_archive(split_dir, split_zip_path)
        logger.info("Job %d: split cards archive created: %s", job_id, split_zip_path)

        # ── Step 6: Compare BOM vs cards ────────────────────────
        sync_repository.update_job_status(job_id, "processing", stage="comparing")
        logger.info("Job %d: comparing BOM vs cards...", job_id)

        from burlak_parser.comparator import compare_all_configs, verify_integrity

        # Check if user selected specific configs
        selected_configs = sync_repository.get_selected_configs(job_id)
        if selected_configs:
            logger.info("Job %d: comparing %d selected configs", job_id, len(selected_configs))
            # Filter BOM to only selected configs
            bom.config_names = [cn for cn in bom.config_names if cn in selected_configs]
            logger.info("Job %d: filtered to %d configs", job_id, len(bom.config_names))
        result = compare_all_configs(bom, cards, use_fuzzy=True)

        integrity = verify_integrity(result)
        if not integrity.is_ok:
            logger.warning(
                "Job %d: integrity check failed: config_issues=%s, global_issue=%s",
                job_id,
                integrity.config_issues,
                integrity.global_issue,
            )
        else:
            logger.info("Job %d: integrity check passed (%d/%d configs OK)",
                        job_id, integrity.configs_ok, integrity.total_configs)

        logger.info(
            "Job %d: comparison done: %d discrepancies across %d configs",
            job_id,
            len(result.all_discrepancies),
            result.total_configs,
        )

        # ── Step 7: Generate diff.xlsx report ───────────────────
        sync_repository.update_job_status(job_id, "processing", stage="generating_report")
        logger.info("Job %d: generating diff.xlsx...", job_id)

        diff_path = os.path.join(job_dir, "diff.xlsx")

        from burlak_parser.report_generator import generate_discrepancy_report

        generate_discrepancy_report(
            result, diff_path, bom=bom, cards_data=cards,
        )
        logger.info("Job %d: report saved: %s", job_id, diff_path)

        # ── Step 8: Update progress in DB ──────────────────────
        sync_repository.update_job_progress(
            job_id,
            total=cards.total_cards_processed,
            processed=cards.total_cards_processed,
            failed=len(cards.corrupted_files),
        )

        # ── Step 9: Set final status ────────────────────────────
        if cards.corrupted_files:
            error_msg = f"Completed with {len(cards.corrupted_files)} corrupted files"
            sync_repository.update_job_status(
                job_id,
                "error",
                stage="completed_with_errors",
                error=error_msg,
            )
            logger.warning("Job %d: %s", job_id, error_msg)
        else:
            sync_repository.update_job_status(job_id, "done", "completed")
            logger.info("Job %d: heuristic processing completed successfully", job_id)

    except Exception as exc:
        logger.error(
            "Heuristic processing failed for job %d: %s",
            job_id, exc, exc_info=True,
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        else:
            sync_repository.update_job_status(
                job_id,
                "error",
                stage="heuristic_processing_failed",
                error=str(exc),
            )
    finally:
        # Clean up temp directory
        if tmp_dir and os.path.isdir(tmp_dir):
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception as e:
                logger.debug("Failed to clean up temp dir %s: %s", tmp_dir, e)


def _extract_archive(archive_path: str, extract_dir: str) -> None:
    """Extract a ZIP archive to a target directory.

    Supports multiple encodings for Chinese file names (GBK).

    Args:
        archive_path: Path to the ZIP file.
        extract_dir: Destination directory.

    Raises:
        ValueError: If the archive cannot be opened.
    """
    import zipfile

    os.makedirs(extract_dir, exist_ok=True)

    for enc in ("gbk", "utf-8", "cp1251", "latin-1", None):
        try:
            kwargs = {}
            if enc is not None:
                kwargs["metadata_encoding"] = enc
            with zipfile.ZipFile(archive_path, "r", **kwargs) as zf:
                zf.extractall(extract_dir)
            logger.info(
                "Archive extracted (%d files) using encoding=%s",
                len(os.listdir(extract_dir)),
                enc or "default",
            )
            return
        except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError) as e:
            logger.debug("ZIP extraction with %s failed: %s", enc, e)
            continue

    raise ValueError(f"Failed to extract ZIP archive (tried all encodings): {archive_path}")
