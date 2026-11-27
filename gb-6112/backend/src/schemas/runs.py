from pydantic import BaseModel, Field
from src.constants.job_state import JobState


class RunInput(BaseModel):
    parameter: int = Field(strict=True, ge=1)


class CancelInput(BaseModel):
    reason: str | None = Field(default=None, max_length=320)


class RunEntry(BaseModel):
    id: int
    case_id: int
    case_title: str
    status: JobState
    parameter: int
    actor: str
    created_at: int
    finished_at: int | None
    result: dict | None
    failure: str | None
    digest: str | None
    cancel_reason: str | None = None
    cancelled_by: str | None = None
