from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.base import utcnow
from app.models.task import TERMINAL_TASK_RUN_STATUSES, Task, TaskRun
from app.repositories.task_runs import TaskRunRepository
from app.task_engine.transitions import TERMINAL_TASK_STATUSES, validate_transition

TASK_TO_RUN_TERMINAL_STATUS = {
    "success": "success",
    "failed": "failed",
    "retry": "failed",
    "waiting_credential": "blocked",
    "cancelled": "cancelled",
}


class TaskNotFoundError(LookupError):
    pass


class TaskRunNotFoundError(LookupError):
    pass


class TaskStateConflictError(RuntimeError):
    def __init__(self, task_id: int, expected_status: str, actual_status: str) -> None:
        self.task_id = task_id
        self.expected_status = expected_status
        self.actual_status = actual_status
        super().__init__(
            f"task {task_id} expected status {expected_status!r}, found {actual_status!r}"
        )


class ActiveTaskRunExistsError(RuntimeError):
    pass


class TaskRepository:
    """The v2 persistence entrypoint for Task and Task Run state changes."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._runs = TaskRunRepository(session)

    def create_task(self, task: Task) -> Task:
        if task.status is None:
            task.status = "pending"
        if task.status != "pending":
            raise ValueError("a new task must start in pending state")
        self._session.add(task)
        self._session.flush()
        return task

    def get(self, task_id: int, *, include_runs: bool = False) -> Task | None:
        if not include_runs:
            return self._session.get(Task, task_id)
        return self._session.scalar(
            select(Task)
            .options(selectinload(Task.runs))
            .where(Task.id == task_id)
        )

    def transition(
        self,
        task_id: int,
        *,
        expected_status: str,
        target_status: str,
        occurred_at: datetime | None = None,
        blocked_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> Task:
        task = self._require_task(task_id)
        self._validate_expected_status(task, expected_status)
        validate_transition(expected_status, target_status)
        self._apply_task_transition(
            task,
            target_status=target_status,
            occurred_at=occurred_at or utcnow(),
            blocked_reason=blocked_reason,
            error_code=error_code,
            error_message=error_message,
            next_attempt_at=next_attempt_at,
        )
        self._session.flush()
        return task

    def create_run(
        self,
        task_id: int,
        *,
        worker_id: str | None = None,
        lock_token: str | None = None,
        started_at: datetime | None = None,
    ) -> TaskRun:
        task = self._require_task(task_id)
        if task.status != "running":
            raise TaskStateConflictError(task.id, "running", task.status)
        active_run_id = self._session.scalar(
            select(TaskRun.id).where(
                TaskRun.task_id == task_id,
                TaskRun.status == "running",
            )
        )
        if active_run_id is not None:
            raise ActiveTaskRunExistsError(f"task {task_id} already has an active run")
        next_run_number = (
            self._session.scalar(
                select(func.coalesce(func.max(TaskRun.run_number), 0)).where(
                    TaskRun.task_id == task_id
                )
            )
            or 0
        ) + 1
        return self._runs.append(
            TaskRun(
                task_id=task_id,
                run_number=next_run_number,
                worker_id=worker_id,
                lock_token=lock_token,
                status="running",
                started_at=started_at or utcnow(),
            )
        )

    def finish_run(
        self,
        task_id: int,
        task_run_id: int,
        *,
        expected_task_status: str,
        task_status: str,
        run_status: str,
        finished_at: datetime | None = None,
        duration_ms: int | None = None,
        result_summary: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metrics: dict[str, object] | None = None,
        blocked_reason: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> tuple[Task, TaskRun]:
        task = self._require_task(task_id)
        self._validate_expected_status(task, expected_task_status)
        validate_transition(expected_task_status, task_status)
        expected_run_status = TASK_TO_RUN_TERMINAL_STATUS.get(task_status)
        if expected_run_status != run_status or run_status not in TERMINAL_TASK_RUN_STATUSES:
            raise ValueError(
                f"task status {task_status!r} requires run status {expected_run_status!r}"
            )

        task_run = self._runs.get(task_run_id)
        if task_run is None or task_run.task_id != task_id:
            raise TaskRunNotFoundError(f"task run {task_run_id} not found for task {task_id}")
        if task_run.status != "running":
            raise ValueError("only an active task run can be finished")

        now = finished_at or utcnow()
        self._apply_task_transition(
            task,
            target_status=task_status,
            occurred_at=now,
            blocked_reason=blocked_reason,
            error_code=error_code,
            error_message=error_message,
            next_attempt_at=next_attempt_at,
        )
        task_run.status = run_status
        task_run.finished_at = now
        task_run.duration_ms = duration_ms
        task_run.result_summary = result_summary
        task_run.error_code = error_code
        task_run.error_message = error_message
        if metrics is not None:
            task_run.metrics = metrics
        self._session.flush()
        return task, task_run

    def _require_task(self, task_id: int) -> Task:
        task = self.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        return task

    @staticmethod
    def _validate_expected_status(task: Task, expected_status: str) -> None:
        if task.status != expected_status:
            raise TaskStateConflictError(task.id, expected_status, task.status)

    @staticmethod
    def _apply_task_transition(
        task: Task,
        *,
        target_status: str,
        occurred_at: datetime,
        blocked_reason: str | None,
        error_code: str | None,
        error_message: str | None,
        next_attempt_at: datetime | None,
    ) -> None:
        task.status = target_status
        task.last_error_code = error_code
        task.last_error_message = error_message
        task.next_attempt_at = next_attempt_at if target_status == "retry" else None

        if target_status == "waiting_credential":
            task.blocked_reason = blocked_reason
            task.blocked_at = occurred_at
        elif target_status != "cancel_requested":
            task.blocked_reason = None
            task.blocked_at = None

        if target_status == "cancel_requested" and task.cancel_requested_at is None:
            task.cancel_requested_at = occurred_at

        task.completed_at = occurred_at if target_status in TERMINAL_TASK_STATUSES else None
