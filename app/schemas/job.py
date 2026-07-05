from datetime import datetime
from typing import Any

from pydantic import BaseModel


class JobCreateRequest(BaseModel):
    """Request body for POST /api/v1/jobs"""

    mode: str = "heuristic"


class JobCreateResponse(BaseModel):
    """Response for POST /api/v1/jobs"""

    id: int
    mode: str
    status: str
    created_at: datetime
    session_token: str
    selected_config: str | None = None


class JobStatusResponse(BaseModel):
    """Response for GET /api/v1/jobs/{job_id}"""

    id: int
    mode: str = "heuristic"
    status: str  # awaiting_upload | processing | done | error
    stage: (
        str | None
    )  # unpacking | analyzing_mapping | processing_cards | aggregating | packaging
    total: int
    processed: int
    failed: int
    error: str | None = None
    bom_uploaded: bool
    archive_uploaded: bool
    created_at: datetime
    updated_at: datetime
    selected_config: str | None = None


class JobStartRequest(BaseModel):
    """Request body for POST /api/v1/jobs/{job_id}/start"""

    selected_config: str | None = None  # deprecated, use selected_configs
    selected_configs: list[str] | None = None


class JobStartResponse(BaseModel):
    """Response for POST /api/v1/jobs/{job_id}/start"""

    message: str
    job_id: int
    status: str
    stage: str | None = None


class JobCancelResponse(BaseModel):
    """Response for POST /api/v1/jobs/{job_id}/cancel"""

    message: str
    job_id: int
    status: str
    task_id: str | None = None


class BomConfigsResponse(BaseModel):
    """Response for POST /api/v1/jobs/{job_id}/parse-bom"""

    config_names: list[str]
    total_parts: int
    total_configs: int


class ErrorResponse(BaseModel):
    """Uniform error response format"""

    error: "ErrorDetail"


class ErrorDetail(BaseModel):
    code: str
    message: str
    detail: Any = None
