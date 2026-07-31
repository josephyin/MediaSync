from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import Task


class TaskRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_number: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    result_summary: str | None
    error_code: str | None
    error_message: str | None


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
    retry_count: int
    max_retries: int
    latest_run: TaskRunRead | None = None

    @classmethod
    def from_task(cls, task: Task) -> "TaskRead":
        latest_run = max(task.runs, key=lambda run: run.run_number, default=None)
        result = cls.model_validate(task)
        if latest_run is None:
            return result.model_copy(
                update={
                    "message": task.message or task.last_error_message,
                    "error_code": task.error_code or task.last_error_code,
                    "finished_at": task.finished_at or task.completed_at,
                }
            )

        message = (
            latest_run.result_summary
            or latest_run.error_message
            or task.message
            or task.last_error_message
        )
        return result.model_copy(
            update={
                "message": message,
                "error_code": (
                    latest_run.error_code
                    or task.error_code
                    or task.last_error_code
                ),
                "attempt_count": task.retry_count,
                "max_attempts": task.max_retries,
                "started_at": latest_run.started_at,
                "finished_at": latest_run.finished_at or task.completed_at,
                "latest_run": TaskRunRead.model_validate(latest_run),
            }
        )
