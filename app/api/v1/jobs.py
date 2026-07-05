import json
from collections.abc import AsyncGenerator

import aiosqlite
from fastapi import APIRouter, Depends, Header, status
from fastapi.responses import StreamingResponse

from app.api.v1.deps import verify_job_session
from app.core.exceptions import JobNotFoundError
from app.db.async_repository import get_job
from app.db.database import get_async_db
from app.schemas.job import (
    BomConfigsResponse,
    JobCancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobStartRequest,
    JobStartResponse,
    JobStatusResponse,
)
from app.services.cache_service import cache_job_status, get_cached_job_status
from app.services.job_creation_service import JobCreationService
from app.services.job_processing_service import JobProcessingService
from app.services.notification_service import subscribe_progress

router = APIRouter(tags=["jobs"])


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_new_job(
    body: JobCreateRequest | None = None,
    db: aiosqlite.Connection = Depends(get_async_db),
) -> JobCreateResponse:
    """Create a new job in awaiting_upload status.

    Args:
        body: Optional request body with mode parameter.
              mode="heuristic" — use heuristic parser (default, no ML needed).
              mode="ml" — use ML service pipeline.
              Defaults to 'heuristic' when body is omitted (backwards-compatible).

    Returns the job ID, mode, initial status, and creation timestamp.
    """
    mode = body.mode if body else "heuristic"
    job = await JobCreationService.create(db, mode=mode)

    return JobCreateResponse(
        id=job["id"],
        mode=job["mode"],
        status=job["status"],
        created_at=job["created_at"],
        session_token=job["session_token"],
    )


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_async_db),
    _session_ok: None = Depends(verify_job_session),
) -> JobStatusResponse:
    """Get job status and progress.

    Reads from Redis cache first (TTL 5s), falls back to SQLite.
    Writes cache on miss (cache-aside pattern).
    """
    cached = await get_cached_job_status(job_id)
    if cached is not None:
        return JobStatusResponse(**cached)

    job = await get_job(db, job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} not found")

    response = JobStatusResponse(
        id=job["id"],
        mode=job.get("mode", "heuristic"),
        status=job["status"],
        stage=job.get("stage"),
        total=job["total"],
        processed=job["processed"],
        failed=job["failed"],
        error=job.get("error"),
        bom_uploaded=bool(job["bom_uploaded"]),
        archive_uploaded=bool(job["archive_uploaded"]),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        selected_config=job.get("selected_config"),
    )

    await cache_job_status(job_id, response.model_dump(mode="json"))

    return response


@router.get("/jobs/{job_id}/stream")
async def stream_progress(
    job_id: int,
    token: str | None = None,
    db: aiosqlite.Connection = Depends(get_async_db),
) -> StreamingResponse:
    """SSE endpoint for real-time job progress via Redis Pub/Sub.

    Uses query-parameter auth (``?token=xxx``) because the browser
    ``EventSource`` API cannot send custom headers.

    Frontend subscribes with:
        const es = new EventSource('/api/v1/jobs/42/stream?token=xxx');
        es.onmessage = (e) => console.log(JSON.parse(e.data));
    """
    # Verify session token via query param (EventSource can't set headers)
    from app.api.v1.deps import _verify_token_sync  # noqa: E402
    await _verify_token_sync(db, job_id, token)

    async def event_generator() -> AsyncGenerator[str, None]:
        pubsub = await subscribe_progress(job_id)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    yield f"data: {message['data']}\n\n"
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/parse-bom")
async def parse_bom_configs(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_async_db),
    _session_ok: None = Depends(verify_job_session),
) -> BomConfigsResponse:
    """Parse BOM and return available configuration names.

    This endpoint is called after BOM upload to let the user
    choose a specific configuration before starting processing.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise JobNotFoundError(f"Job {job_id} not found")

    bom_path = job.get("bom_path")
    if not bom_path:
        raise JobNotFoundError(f"BOM file not uploaded for job {job_id}")

    import os
    if not os.path.isfile(bom_path):
        raise JobNotFoundError(f"BOM file not found on disk: {bom_path}")

    import logging
    logger = logging.getLogger(__name__)

    try:
        from burlak_parser.bom_parser import BOMService
        bom_service = BOMService()
        bom = bom_service.load(bom_path)
    except Exception as exc:
        logger.error("Failed to parse BOM for job %d: %s", job_id, exc, exc_info=True)
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"Failed to parse BOM file: {exc}",
        )

    return BomConfigsResponse(
        config_names=bom.config_names,
        total_parts=len(bom.parts),
        total_configs=len(bom.config_names),
    )


@router.post("/jobs/{job_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_job_processing(
    job_id: int,
    body: JobStartRequest | None = None,
    db: aiosqlite.Connection = Depends(get_async_db),
    _session_ok: None = Depends(verify_job_session),
) -> JobStartResponse:
    """Start processing a job.

    Atomically transitions state and dispatches async work.
    Dispatches heuristic or ML pipeline based on job mode.
    """
    state = await JobProcessingService.transition_to_processing(db, job_id)

    # Save selected_config if provided in request body
    if body:
        configs_to_save: list[str] | None = None
        if body.selected_configs is not None:
            configs_to_save = body.selected_configs
        elif body.selected_config is not None:
            configs_to_save = [body.selected_config]

        if configs_to_save is not None:
            from datetime import UTC, datetime
            now = datetime.now(UTC).isoformat()
            await db.execute(
                "UPDATE jobs SET selected_config = ?, updated_at = ? WHERE id = ?",
                (json.dumps(configs_to_save), now, job_id),
            )
            await db.commit()

    # Fetch the job to determine mode
    job = await get_job(db, job_id)
    mode = job.get("mode", "heuristic") if job else "heuristic"
    JobProcessingService.dispatch_processing(job_id, mode=mode)

    return JobStartResponse(
        message="Job processing started",
        job_id=job_id,
        status=state["status"],
        stage=state.get("stage"),
    )


@router.post("/jobs/{job_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_job_processing(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_async_db),
    _session_ok: None = Depends(verify_job_session),
) -> JobCancelResponse:
    """Cancel a running job.

    Revokes the Celery task, marks the job as cancelled, and cleans up
    storage files. Only jobs in 'processing' status can be cancelled.
    """
    result = await JobProcessingService.cancel_job(db, job_id)
    return JobCancelResponse(
        message="Job cancelled",
        job_id=job_id,
        status=result["status"],
        task_id=result.get("task_id") or None,
    )
