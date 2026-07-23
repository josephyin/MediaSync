import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from app.models.base import utcnow
from app.models.task import TERMINAL_TASK_RUN_STATUSES, Task, TaskRun
from app.task_engine.transitions import TERMINAL_TASK_STATUSES, validate_transition

QUEUE_TASK_STATUSES = ("pending", "retry")
CLAIMABLE_TASK_STATUSES = (*QUEUE_TASK_STATUSES, "cancel_requested")
LEASED_TASK_STATUSES = ("running", "cancel_requested")
LEASE_EXPIRED_ERROR_CODE = "WORKER_LEASE_EXPIRED"
LEASE_EXPIRED_ERROR_MESSAGE = "worker lease expired before task completion"
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


class TaskOwnershipRequiredError(RuntimeError):
    pass


class TaskOwnershipLostError(RuntimeError):
    def __init__(self, task_id: int, worker_id: str) -> None:
        self.task_id = task_id
        self.worker_id = worker_id
        super().__init__(f"worker {worker_id!r} does not own active task {task_id}")


@dataclass(frozen=True)
class TaskClaim:
    task: Task
    task_run: TaskRun
    lock_token: str
    lease_until: datetime


@dataclass(frozen=True)
class ExpiredTaskLease:
    task_id: int
    status: str
    worker_id: str
    lock_token: str
    lease_until: datetime


@dataclass(frozen=True)
class RecoveredTaskLease:
    task: Task
    task_run: TaskRun
    previous_lock_token: str


class TaskRepository:
    """The v2 persistence entrypoint for Task and Task Run state changes."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session = session
        self._clock = clock

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
        if target_status == "running" or (
            expected_status in LEASED_TASK_STATUSES
            and target_status != "cancel_requested"
        ):
            raise TaskOwnershipRequiredError(
                f"task transition {expected_status!r} -> {target_status!r} "
                "requires an ownership-aware repository operation"
            )
        now = occurred_at or self._clock()
        transition_values = self._task_transition_values(
            target_status=target_status,
            occurred_at=now,
            blocked_reason=blocked_reason,
            error_code=error_code,
            error_message=error_message,
            next_attempt_at=next_attempt_at,
        )
        if target_status == "cancel_requested":
            transition_values["cancel_requested_at"] = func.coalesce(
                Task.cancel_requested_at,
                now,
            )
        transitioned_task_id = self._session.scalar(
            update(Task)
            .where(Task.id == task_id, Task.status == expected_status)
            .values(**transition_values, updated_at=now)
            .returning(Task.id)
            .execution_options(synchronize_session=False)
        )
        if transitioned_task_id is None:
            actual = self._require_task(task_id, populate_existing=True)
            raise TaskStateConflictError(task_id, expected_status, actual.status)
        return self._require_task(transitioned_task_id, populate_existing=True)

    def claim_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
        claimed_at: datetime | None = None,
    ) -> TaskClaim | None:
        self._validate_owner_arguments(worker_id, lease_duration)
        now = claimed_at or self._clock()
        lease_until = now + lease_duration
        lock_token = secrets.token_hex(32)
        ownership_is_clear = and_(
            Task.locked_by.is_(None),
            Task.lock_token.is_(None),
            Task.locked_at.is_(None),
            Task.lease_until.is_(None),
        )
        is_claimable = or_(
            and_(
                Task.status.in_(QUEUE_TASK_STATUSES),
                or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now),
                ownership_is_clear,
            ),
            and_(
                Task.status == "cancel_requested",
                ownership_is_clear,
            ),
        )
        candidate_id = (
            select(Task.id)
            .where(is_claimable)
            .order_by(
                Task.priority.desc(),
                func.coalesce(Task.next_attempt_at, Task.created_at).asc(),
                Task.created_at.asc(),
                Task.id.asc(),
            )
            .limit(1)
            .scalar_subquery()
        )
        claim_statement = (
            update(Task)
            .where(
                Task.id == candidate_id,
                Task.status.in_(CLAIMABLE_TASK_STATUSES),
                ownership_is_clear,
                or_(
                    Task.status == "cancel_requested",
                    Task.next_attempt_at.is_(None),
                    Task.next_attempt_at <= now,
                ),
            )
            .values(
                status=case(
                    (Task.status == "cancel_requested", "cancel_requested"),
                    else_="running",
                ),
                locked_by=worker_id,
                lock_token=lock_token,
                locked_at=now,
                lease_until=lease_until,
                next_attempt_at=None,
                updated_at=now,
            )
            .returning(Task.id)
        )

        with self._session.begin_nested():
            task_id = self._session.scalar(
                claim_statement.execution_options(synchronize_session=False)
            )
            if task_id is None:
                return None
            task = self._require_task(task_id, populate_existing=True)
            task_run = self.create_run(
                task_id,
                worker_id=worker_id,
                lock_token=lock_token,
                started_at=now,
                last_heartbeat_at=now,
            )

        return TaskClaim(
            task=task,
            task_run=task_run,
            lock_token=lock_token,
            lease_until=lease_until,
        )

    def create_run(
        self,
        task_id: int,
        *,
        worker_id: str,
        lock_token: str,
        started_at: datetime | None = None,
        last_heartbeat_at: datetime | None = None,
    ) -> TaskRun:
        task = self._require_task(task_id)
        if task.status not in LEASED_TASK_STATUSES:
            raise TaskStateConflictError(
                task.id,
                "running or cancel_requested",
                task.status,
            )
        if task.locked_by != worker_id or task.lock_token != lock_token:
            raise TaskOwnershipLostError(task_id, worker_id)
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
        return self._append_task_run(
            TaskRun(
                task_id=task_id,
                run_number=next_run_number,
                worker_id=worker_id,
                lock_token=lock_token,
                status="running",
                started_at=started_at or self._clock(),
                last_heartbeat_at=last_heartbeat_at,
            )
        )

    def heartbeat(
        self,
        task_id: int,
        *,
        worker_id: str,
        lock_token: str,
        lease_duration: timedelta,
        heartbeat_at: datetime | None = None,
    ) -> tuple[Task, TaskRun]:
        self._validate_owner_arguments(worker_id, lease_duration)
        if not lock_token:
            raise ValueError("lock_token must not be empty")
        now = heartbeat_at or self._clock()
        lease_until = now + lease_duration

        with self._session.begin_nested():
            task_id_result = self._session.scalar(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status.in_(LEASED_TASK_STATUSES),
                    Task.locked_by == worker_id,
                    Task.lock_token == lock_token,
                    Task.lease_until > now,
                )
                .values(lease_until=lease_until, updated_at=now)
                .returning(Task.id)
                .execution_options(synchronize_session=False)
            )
            if task_id_result is None:
                raise TaskOwnershipLostError(task_id, worker_id)

            task_run_id = self._session.scalar(
                update(TaskRun)
                .where(
                    TaskRun.task_id == task_id,
                    TaskRun.status == "running",
                    TaskRun.worker_id == worker_id,
                    TaskRun.lock_token == lock_token,
                )
                .values(last_heartbeat_at=now, updated_at=now)
                .returning(TaskRun.id)
                .execution_options(synchronize_session=False)
            )
            if task_run_id is None:
                raise TaskOwnershipLostError(task_id, worker_id)

        return (
            self._require_task(task_id, populate_existing=True),
            self._require_task_run(task_run_id, populate_existing=True),
        )

    def finish_run(
        self,
        task_id: int,
        task_run_id: int,
        *,
        worker_id: str,
        lock_token: str,
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
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if not lock_token:
            raise ValueError("lock_token must not be empty")
        validate_transition(expected_task_status, task_status)
        expected_run_status = TASK_TO_RUN_TERMINAL_STATUS.get(task_status)
        if expected_run_status != run_status or run_status not in TERMINAL_TASK_RUN_STATUSES:
            raise ValueError(
                f"task status {task_status!r} requires run status {expected_run_status!r}"
            )

        ownership_checked_at = self._clock()
        outcome_at = finished_at or ownership_checked_at
        task_values = self._task_transition_values(
            target_status=task_status,
            occurred_at=outcome_at,
            blocked_reason=blocked_reason,
            error_code=error_code,
            error_message=error_message,
            next_attempt_at=next_attempt_at,
        )
        task_values.update(
            {
                "locked_by": None,
                "lock_token": None,
                "locked_at": None,
                "lease_until": None,
                "updated_at": ownership_checked_at,
            }
        )
        run_values: dict[str, object] = {
            "status": run_status,
            "finished_at": outcome_at,
            "duration_ms": duration_ms,
            "result_summary": result_summary,
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": ownership_checked_at,
        }
        if metrics is not None:
            run_values["metrics"] = metrics

        with self._session.begin_nested():
            persisted_task_id = self._session.scalar(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status == expected_task_status,
                    Task.locked_by == worker_id,
                    Task.lock_token == lock_token,
                    Task.lease_until > ownership_checked_at,
                )
                .values(**task_values)
                .returning(Task.id)
                .execution_options(synchronize_session=False)
            )
            if persisted_task_id is None:
                raise TaskOwnershipLostError(task_id, worker_id)

            persisted_run_id = self._session.scalar(
                update(TaskRun)
                .where(
                    TaskRun.id == task_run_id,
                    TaskRun.task_id == task_id,
                    TaskRun.status == "running",
                    TaskRun.worker_id == worker_id,
                    TaskRun.lock_token == lock_token,
                )
                .values(**run_values)
                .returning(TaskRun.id)
                .execution_options(synchronize_session=False)
            )
            if persisted_run_id is None:
                raise TaskRunNotFoundError(
                    f"active task run {task_run_id} not found for owned task {task_id}"
                )

        return (
            self._require_task(persisted_task_id, populate_existing=True),
            self._require_task_run(persisted_run_id, populate_existing=True),
        )

    def list_expired_leases(
        self,
        *,
        limit: int = 100,
        expired_at: datetime | None = None,
    ) -> list[ExpiredTaskLease]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        now = expired_at or self._clock()
        rows = self._session.execute(
            select(
                Task.id,
                Task.status,
                Task.locked_by,
                Task.lock_token,
                Task.lease_until,
            )
            .where(
                Task.status.in_(LEASED_TASK_STATUSES),
                Task.locked_by.is_not(None),
                Task.lock_token.is_not(None),
                Task.lease_until.is_not(None),
                Task.lease_until <= now,
            )
            .order_by(Task.lease_until.asc(), Task.priority.desc(), Task.id.asc())
            .limit(limit)
        )
        leases: list[ExpiredTaskLease] = []
        for row in rows:
            if row.locked_by is None or row.lock_token is None or row.lease_until is None:
                continue
            leases.append(
                ExpiredTaskLease(
                    task_id=row.id,
                    status=row.status,
                    worker_id=row.locked_by,
                    lock_token=row.lock_token,
                    lease_until=row.lease_until,
                )
            )
        return leases

    def recover_expired_lease(
        self,
        lease: ExpiredTaskLease,
        *,
        next_attempt_at: datetime | None = None,
        recovered_at: datetime | None = None,
    ) -> RecoveredTaskLease | None:
        now = recovered_at or self._clock()
        current = self._session.execute(
            select(
                Task.status,
                Task.retry_count,
                Task.max_retries,
            ).where(
                Task.id == lease.task_id,
                Task.status == lease.status,
                Task.locked_by == lease.worker_id,
                Task.lock_token == lease.lock_token,
                Task.lease_until <= now,
            )
        ).one_or_none()
        if current is None:
            return None

        retry_count = current.retry_count + 1
        if current.status == "cancel_requested":
            target_status = "cancel_requested"
            retry_at = None
        elif retry_count > current.max_retries:
            target_status = "failed"
            retry_at = None
        else:
            if next_attempt_at is None:
                raise ValueError(
                    "next_attempt_at is required when an expired task remains retryable"
                )
            target_status = "retry"
            retry_at = next_attempt_at

        task_values = self._task_transition_values(
            target_status=target_status,
            occurred_at=now,
            blocked_reason=None,
            error_code=LEASE_EXPIRED_ERROR_CODE,
            error_message=LEASE_EXPIRED_ERROR_MESSAGE,
            next_attempt_at=retry_at,
        )
        if target_status == "cancel_requested":
            task_values.pop("cancel_requested_at", None)
        task_values.update(
            {
                "retry_count": retry_count,
                "locked_by": None,
                "lock_token": None,
                "locked_at": None,
                "lease_until": None,
                "updated_at": now,
            }
        )

        with self._session.begin_nested():
            recovered_task_id = self._session.scalar(
                update(Task)
                .where(
                    Task.id == lease.task_id,
                    Task.status == lease.status,
                    Task.locked_by == lease.worker_id,
                    Task.lock_token == lease.lock_token,
                    Task.lease_until <= now,
                )
                .values(**task_values)
                .returning(Task.id)
                .execution_options(synchronize_session=False)
            )
            if recovered_task_id is None:
                return None

            recovered_run_id = self._mark_task_run_lost(
                task_id=lease.task_id,
                worker_id=lease.worker_id,
                lock_token=lease.lock_token,
                recovered_at=now,
            )
            if recovered_run_id is None:
                raise TaskRunNotFoundError(
                    f"active task run not found for expired task {lease.task_id}"
                )

        return RecoveredTaskLease(
            task=self._require_task(recovered_task_id, populate_existing=True),
            task_run=self._require_task_run(
                recovered_run_id,
                populate_existing=True,
            ),
            previous_lock_token=lease.lock_token,
        )

    def _require_task(self, task_id: int, *, populate_existing: bool = False) -> Task:
        if populate_existing:
            task = self._session.scalar(
                select(Task)
                .where(Task.id == task_id)
                .execution_options(populate_existing=True)
            )
        else:
            task = self.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task {task_id} not found")
        return task

    def _require_task_run(
        self,
        task_run_id: int,
        *,
        populate_existing: bool = False,
    ) -> TaskRun:
        if populate_existing:
            task_run = self._session.scalar(
                select(TaskRun)
                .where(TaskRun.id == task_run_id)
                .execution_options(populate_existing=True)
            )
        else:
            task_run = self._session.get(TaskRun, task_run_id)
        if task_run is None:
            raise TaskRunNotFoundError(f"task run {task_run_id} not found")
        return task_run

    def _append_task_run(self, task_run: TaskRun) -> TaskRun:
        if task_run.status != "running":
            raise ValueError("a new task run must start in running state")
        self._session.add(task_run)
        self._session.flush()
        return task_run

    def _mark_task_run_lost(
        self,
        *,
        task_id: int,
        worker_id: str,
        lock_token: str,
        recovered_at: datetime,
    ) -> int | None:
        return self._session.scalar(
            update(TaskRun)
            .where(
                TaskRun.task_id == task_id,
                TaskRun.status == "running",
                TaskRun.worker_id == worker_id,
                TaskRun.lock_token == lock_token,
            )
            .values(
                status="lost",
                finished_at=recovered_at,
                result_summary=LEASE_EXPIRED_ERROR_MESSAGE,
                error_code=LEASE_EXPIRED_ERROR_CODE,
                error_message=LEASE_EXPIRED_ERROR_MESSAGE,
                updated_at=recovered_at,
            )
            .returning(TaskRun.id)
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    def _validate_owner_arguments(worker_id: str, lease_duration: timedelta) -> None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")

    @staticmethod
    def _validate_expected_status(task: Task, expected_status: str) -> None:
        if task.status != expected_status:
            raise TaskStateConflictError(task.id, expected_status, task.status)

    @staticmethod
    def _task_transition_values(
        *,
        target_status: str,
        occurred_at: datetime,
        blocked_reason: str | None,
        error_code: str | None,
        error_message: str | None,
        next_attempt_at: datetime | None,
    ) -> dict[str, object | None]:
        values: dict[str, object | None] = {
            "status": target_status,
            "last_error_code": error_code,
            "last_error_message": error_message,
            "next_attempt_at": next_attempt_at if target_status == "retry" else None,
            "completed_at": (
                occurred_at if target_status in TERMINAL_TASK_STATUSES else None
            ),
        }
        if target_status == "waiting_credential":
            values["blocked_reason"] = blocked_reason
            values["blocked_at"] = occurred_at
        elif target_status != "cancel_requested":
            values["blocked_reason"] = None
            values["blocked_at"] = None

        if target_status == "cancel_requested":
            values["cancel_requested_at"] = occurred_at

        return values
