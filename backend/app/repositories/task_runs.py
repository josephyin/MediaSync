from datetime import datetime

from sqlalchemy.orm import Session

from app.models.task import TERMINAL_TASK_RUN_STATUSES, TaskRun


class TaskRunRepository:
    """Persistence boundary for append-only task execution attempts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, task_run: TaskRun) -> TaskRun:
        if task_run.status != "running":
            raise ValueError("a new task run must start in running state")
        self._session.add(task_run)
        self._session.flush()
        return task_run

    def get(self, task_run_id: int) -> TaskRun | None:
        return self._session.get(TaskRun, task_run_id)

    def finalize(
        self,
        task_run_id: int,
        *,
        status: str,
        finished_at: datetime,
        duration_ms: int | None = None,
        result_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metrics: dict[str, object] | None = None,
    ) -> TaskRun:
        if status not in TERMINAL_TASK_RUN_STATUSES:
            raise ValueError(f"invalid terminal task run status: {status}")

        task_run = self.get(task_run_id)
        if task_run is None:
            raise LookupError(f"task run {task_run_id} not found")
        if task_run.status != "running":
            raise ValueError("only an active task run can be finalized")

        task_run.status = status
        task_run.finished_at = finished_at
        task_run.duration_ms = duration_ms
        task_run.result_summary = result_summary
        task_run.error_code = error_code
        task_run.error_message = error_message
        if metrics is not None:
            task_run.metrics = metrics
        self._session.flush()
        return task_run
