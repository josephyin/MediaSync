from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subscription_id: int | None
    file_id: int | None
    type: str
    trigger_type: str
    status: str
    idempotency_key: str | None
    message: str | None
    error_code: str | None
    attempt_count: int
    max_attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    next_attempt_at: datetime | None
    updated_at: datetime
