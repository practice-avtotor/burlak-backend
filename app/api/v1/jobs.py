from collections.abc import AsyncGenerator

import aiosqlite
from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.core.exceptions import JobNotFoundError
from app.db.async_repository import get_job
from app.db.database import get_async_db
from app.schemas.job import JobCreateResponse, JobStartResponse, JobStatusResponse
from app.services.cache_service import cache_job_status, get_cached_job_status
from app.services.job_creation_service import JobCreationService
from app.services.job_processing_service import JobProcessingService
from app.services.notification_service import subscribe_progress

router = APIRouter(tags=["jobs"])


@router.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_new_job(
    db: aiosqlite.Connection = Depends(get_async_db),
) -> JobCreateResponse:
    """Create a new job in awaiting_upload status.

    Returns the job ID, initial status, and creation timestamp.
    """
    job = await JobCreationService.create(db)

    return JobCreateResponse(
        id=job["id"],
        status=job["status"],
        created_at=job["created_at"],
    )


@router.get("/jobs/{job_id}")
async def get_job_status(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_async_db),
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
        status=job["status"],
        stage=job.get("stage"),
        total=job["total"],
        processed=job["processed"],
        failed=job["failed"],
        bom_uploaded=bool(job["bom_uploaded"]),
        archive_uploaded=bool(job["archive_uploaded"]),
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )

    await cache_job_status(job_id, response.model_dump(mode="json"))

    return response


@router.get("/jobs/{job_id}/stream")
async def stream_progress(job_id: int) -> StreamingResponse:
    """SSE endpoint for real-time job progress via Redis Pub/Sub.

    Frontend subscribes with:
        const es = new EventSource('/api/v1/jobs/42/stream');
        es.onmessage = (e) => console.log(JSON.parse(e.data));
    """

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


@router.post("/jobs/{job_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_job_processing(
    job_id: int,
    db: aiosqlite.Connection = Depends(get_async_db),
) -> JobStartResponse:
    """Start processing a job.

    Validates preconditions, transitions state, and dispatches async work.
    """
    await JobProcessingService.validate_can_start(db, job_id)
    state = await JobProcessingService.transition_to_processing(db, job_id)
    JobProcessingService.dispatch_processing(job_id)

    return JobStartResponse(
        message="Job processing started",
        job_id=job_id,
        status=state["status"],
        stage=state.get("stage"),
    )
