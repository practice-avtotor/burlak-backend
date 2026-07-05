import logging
import os
import zipfile

from celery import Task  # type: ignore[import-untyped]

from app.core.config import get_settings
from app.db import sync_repository
from app.services.snapshot_service import extract_snapshot_from_bytes, group_by_format
from app.services.structure_adapter import ManualResponseNeededError, StructureAdapter
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True, retry_backoff_max=30)  # type: ignore[untyped-decorator]
def analyze_mapping(self: Task, job_id: int) -> None:
    """Extracts Excel snapshots, invokes ML service structure analyzer, and schedules card processing."""
    logger.info(f"Starting analyze_mapping task for job {job_id}")
    try:
        # 1. Retrieve job file paths
        bom_path, archive_path = sync_repository.get_job_files(job_id)
        if not bom_path or not os.path.exists(bom_path):
            raise FileNotFoundError(f"BOM file not found: {bom_path}")
        if not archive_path or not os.path.exists(archive_path):
            raise FileNotFoundError(f"Archive file not found: {archive_path}")

        # 2. Extract BOM snapshot
        with open(bom_path, "rb") as f:
            bom_data = f.read()
        bom_snapshot = extract_snapshot_from_bytes(
            bom_data, os.path.basename(bom_path), max_rows=300
        )
        bom_snapshot["format_group"] = "BOM_standard"

        # 3. Retrieve and group cards
        card_paths = sync_repository.get_card_paths(job_id)
        if not card_paths:
            logger.warning(
                f"No cards found for job {job_id} in DB. Transitioning to processing_cards stage."
            )
            sync_repository.update_job_status(job_id, "processing", "processing_cards")
            from app.worker.tasks.aggregate import aggregate

            aggregate.delay(job_id)
            return

        card_groups = group_by_format(card_paths)
        card_snapshots = []

        # 4. Extract snapshot for the representative card of each format group
        with zipfile.ZipFile(archive_path, "r") as zf:
            for group_name, paths in card_groups.items():
                if not paths:
                    continue
                representative_path = paths[0]
                try:
                    card_data = zf.read(representative_path)
                    snapshot = extract_snapshot_from_bytes(
                        card_data, os.path.basename(representative_path), max_rows=300
                    )
                    snapshot["format_group"] = group_name
                    card_snapshots.append(snapshot)
                except Exception as e:
                    logger.error(
                        f"Failed to read representative card {representative_path} for group {group_name}: {e}"
                    )

        # 5. Invoke ML Service to get mapping config
        logger.info(f"Invoking ML analyze-structure endpoint for job {job_id}")

        payload = {
            "bom": [bom_snapshot],
            "sample_cards": card_snapshots,
            "options": {
                "job_id": job_id,
                "max_sample_rows": 300,
                "total_cards_in_archive": len(card_paths),
            },
        }
        with StructureAdapter(settings.ml_service_url) as ml_client:
            mapping_config = ml_client.analyze_structure(payload)

        if "bom" not in mapping_config or "cards" not in mapping_config:
            raise ValueError("ML service returned invalid mapping configuration")

        # 6. Save mapping config to database
        sync_repository.update_mapping_config(job_id, mapping_config)

        # 7. Transition job to processing_cards stage
        sync_repository.update_job_status(job_id, "processing", "processing_cards")

        # 8. Dispatch processing tasks for each card
        from app.worker.tasks.process_card import process_card

        for card_path in card_paths:
            process_card.delay(job_id, card_path)

    except ManualResponseNeededError as exc:
        # Manual mode: no pre-prepared response file. Don't retry.
        detail = exc.detail
        # Use the message from ml-mock if available
        ml_message = detail.get("message", str(exc)) if isinstance(detail, dict) else str(exc)
        error_msg = (
            f"AI-анализ не выполнен (job {job_id}). "
            f"{ml_message}"
        )
        logger.error(error_msg)
        sync_repository.update_job_status(
            job_id,
            "error",
            stage="ml_manual_response_needed",
            error=error_msg,
        )
        return  # Don't retry — manual action required

    except Exception as exc:
        logger.error(f"Analyze mapping failed for job {job_id}: {exc}", exc_info=True)
        if self.request.retries >= self.max_retries:
            sync_repository.update_job_status(job_id, "error", error=str(exc))
            return
        raise self.retry(exc=exc)
