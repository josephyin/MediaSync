import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Task, TaskRun
from app.repositories import TaskRepository
from app.task_engine import (
    TaskExecutionContext,
    TaskHandlerRegistry,
    TaskOutcome,
)
from app.task_engine.worker import (
    ExponentialBackoff,
    InvalidTaskOutcomeError,
    WorkerRuntime,
)

NOW = datetime(2026, 7, 24, 8, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "worker-runtime.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def enqueue(
    sessions: sessionmaker[Session],
    *,
    task_type: str = "test",
    payload_version: int = 1,
    payload: dict[str, object] | None = None,
    max_retries: int = 3,
    priority: int = 0,
) -> int:
    with sessions() as session, session.begin():
        task = TaskRepository(session).create_task(
            Task(
                type=task_type,
                payload_version=payload_version,
                payload=payload or {},
                max_retries=max_retries,
                priority=priority,
            )
        )
        return task.id


def load_task_and_runs(
    sessions: sessionmaker[Session],
    task_id: int,
) -> tuple[Task, list[TaskRun]]:
    with sessions() as session:
        task = session.get(Task, task_id)
        runs = list(
            session.scalars(
                select(TaskRun)
                .where(TaskRun.task_id == task_id)
                .order_by(TaskRun.run_number)
            )
        )
        assert task is not None
        session.expunge(task)
        for run in runs:
            session.expunge(run)
        return task, runs


def runtime(
    sessions: sessionmaker[Session],
    handlers: TaskHandlerRegistry,
    *,
    clock=lambda: NOW,
    lease_duration: timedelta = timedelta(seconds=60),
    heartbeat_interval: timedelta = timedelta(seconds=20),
    backoff: ExponentialBackoff | None = None,
) -> WorkerRuntime:
    return WorkerRuntime(
        session_factory=sessions,
        handlers=handlers,
        worker_id="worker-a",
        lease_duration=lease_duration,
        heartbeat_interval=heartbeat_interval,
        backoff=backoff,
        clock=clock,
    )


async def test_successful_handler_finishes_task_and_run(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(
        sessions,
        payload={"name": "media", "folders": [{"name": "season-1"}]},
    )
    handlers = TaskHandlerRegistry()

    async def handler(context: TaskExecutionContext) -> TaskOutcome:
        assert context.task.task_id == task_id
        assert context.task.payload["name"] == "media"
        with pytest.raises(TypeError):
            context.task.payload["name"] = "mutated"  # type: ignore[index]
        folders = context.task.payload["folders"]
        assert isinstance(folders, tuple)
        with pytest.raises(TypeError):
            folders[0]["name"] = "mutated"  # type: ignore[index]
        return TaskOutcome(
            status="success",
            summary="saved",
            metrics={"files": 1},
        )

    handlers.register("test", 1, handler)

    result = await runtime(sessions, handlers).run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.status == "completed"
    assert result.task_status == "success"
    assert task.status == "success"
    assert task.retry_count == 0
    assert task.lock_token is None
    assert len(runs) == 1
    assert runs[0].status == "success"
    assert runs[0].result_summary == "saved"
    assert runs[0].metrics == {"files": 1}


async def test_retryable_outcome_uses_bounded_backoff(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        return TaskOutcome(
            status="retry",
            error_code="TEMPORARY_FAILURE",
            retry_after=timedelta(minutes=30),
        )

    handlers.register("test", 1, handler)
    worker = runtime(
        sessions,
        handlers,
        backoff=ExponentialBackoff(
            base_delay=timedelta(seconds=10),
            max_delay=timedelta(minutes=5),
        ),
    )

    result = await worker.run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.task_status == "retry"
    assert task.status == "retry"
    assert task.retry_count == 1
    assert task.next_attempt_at == (NOW + timedelta(minutes=5)).replace(tzinfo=None)
    assert runs[0].status == "failed"
    assert runs[0].error_code == "TEMPORARY_FAILURE"


async def test_retry_budget_exhaustion_finishes_as_failed(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions, max_retries=0)
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        return TaskOutcome(status="retry", error_code="TEMPORARY_FAILURE")

    handlers.register("test", 1, handler)

    result = await runtime(sessions, handlers).run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.task_status == "failed"
    assert task.status == "failed"
    assert task.retry_count == 1
    assert task.next_attempt_at is None
    assert runs[0].status == "failed"


async def test_unsupported_handler_is_a_normalized_terminal_failure(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions, task_type="unknown", payload_version=7)

    result = await runtime(sessions, TaskHandlerRegistry()).run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.task_status == "failed"
    assert task.status == "failed"
    assert task.retry_count == 1
    assert task.last_error_code == "UNSUPPORTED_TASK_HANDLER"
    assert runs[0].error_code == "UNSUPPORTED_TASK_HANDLER"


async def test_handler_exception_does_not_persist_raw_exception_text(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        raise RuntimeError("refresh_token=secret")

    handlers.register("test", 1, handler)

    await runtime(sessions, handlers).run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert task.status == "failed"
    assert task.last_error_code == "TASK_HANDLER_EXCEPTION"
    assert task.last_error_message == "task handler raised RuntimeError"
    assert "secret" not in (runs[0].error_message or "")


async def test_waiting_credential_does_not_consume_retry_budget(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        return TaskOutcome(
            status="waiting_credential",
            error_code="TOKEN_EXPIRED",
            blocked_reason="account credential requires user action",
        )

    handlers.register("test", 1, handler)

    result = await runtime(sessions, handlers).run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.task_status == "waiting_credential"
    assert task.status == "waiting_credential"
    assert task.retry_count == 0
    assert task.blocked_reason == "account credential requires user action"
    assert runs[0].status == "blocked"


@pytest.mark.parametrize("outcome_status", ["success", "cancelled"])
async def test_cancel_requested_task_is_reconciled_by_current_owner(
    sessions: sessionmaker[Session],
    outcome_status: str,
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()
    handler_started = asyncio.Event()
    cancellation_committed = asyncio.Event()

    async def handler(context: TaskExecutionContext) -> TaskOutcome:
        handler_started.set()
        await cancellation_committed.wait()
        assert await context.cancellation_requested()
        return TaskOutcome(status=outcome_status)  # type: ignore[arg-type]

    handlers.register("test", 1, handler)
    cycle = asyncio.create_task(runtime(sessions, handlers).run_once())
    await handler_started.wait()
    with sessions() as session, session.begin():
        TaskRepository(session).transition(
            task_id,
            expected_status="running",
            target_status="cancel_requested",
            occurred_at=NOW,
        )
    cancellation_committed.set()

    result = await cycle
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.task_status == outcome_status
    assert task.status == outcome_status
    assert runs[0].status == outcome_status


async def test_cancel_requested_task_rejects_unreconciled_outcome(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        handler_started.set()
        await handler_release.wait()
        return TaskOutcome(status="retry", error_code="REMOTE_OUTCOME_UNKNOWN")

    handlers.register("test", 1, handler)
    cycle = asyncio.create_task(runtime(sessions, handlers).run_once())
    await handler_started.wait()
    with sessions() as session, session.begin():
        TaskRepository(session).transition(
            task_id,
            expected_status="running",
            target_status="cancel_requested",
            occurred_at=NOW,
        )
    handler_release.set()

    with pytest.raises(InvalidTaskOutcomeError, match="must reconcile"):
        await cycle
    task, runs = load_task_and_runs(sessions, task_id)

    assert task.status == "cancel_requested"
    assert task.lock_token is not None
    assert runs[0].status == "running"


async def test_heartbeat_keeps_long_running_handler_owned(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        await asyncio.sleep(0.35)
        return TaskOutcome(status="success")

    handlers.register("test", 1, handler)
    worker = runtime(
        sessions,
        handlers,
        clock=lambda: datetime.now(UTC),
        lease_duration=timedelta(milliseconds=200),
        heartbeat_interval=timedelta(milliseconds=30),
    )

    result = await worker.run_once()
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.status == "completed"
    assert task.status == "success"
    assert runs[0].status == "success"
    assert runs[0].last_heartbeat_at is not None


async def test_stale_worker_cannot_finalize_after_token_changes(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    handlers = TaskHandlerRegistry()
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        handler_started.set()
        await handler_release.wait()
        return TaskOutcome(status="success")

    handlers.register("test", 1, handler)
    cycle = asyncio.create_task(runtime(sessions, handlers).run_once())
    await handler_started.wait()
    with sessions() as session, session.begin():
        session.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(lock_token="replacement-token")
        )
    handler_release.set()

    result = await cycle
    task, runs = load_task_and_runs(sessions, task_id)

    assert result.status == "ownership_lost"
    assert task.status == "running"
    assert task.lock_token == "replacement-token"
    assert runs[0].status == "running"


async def test_expired_lease_is_recovered_before_next_claim(
    sessions: sessionmaker[Session],
) -> None:
    expired_task_id = enqueue(sessions, priority=0)
    old_time = NOW - timedelta(minutes=10)
    with sessions() as session, session.begin():
        claim = TaskRepository(session, clock=lambda: old_time).claim_next(
            worker_id="worker-old",
            lease_duration=timedelta(seconds=30),
            claimed_at=old_time,
        )
        assert claim is not None
        expired_run_id = claim.task_run.id
    next_task_id = enqueue(sessions, priority=10)
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        return TaskOutcome(status="success")

    handlers.register("test", 1, handler)

    result = await runtime(
        sessions,
        handlers,
        backoff=ExponentialBackoff(
            base_delay=timedelta(seconds=10),
            max_delay=timedelta(minutes=1),
        ),
    ).run_once()
    expired_task, expired_runs = load_task_and_runs(sessions, expired_task_id)
    next_task, _next_runs = load_task_and_runs(sessions, next_task_id)

    assert result.recovered_count == 1
    assert result.task_id == next_task_id
    assert expired_task.status == "retry"
    assert expired_task.retry_count == 1
    assert expired_task.next_attempt_at == (NOW + timedelta(seconds=10)).replace(
        tzinfo=None
    )
    assert expired_runs[0].id == expired_run_id
    assert expired_runs[0].status == "lost"
    assert next_task.status == "success"


async def test_run_stops_before_claiming_when_stop_is_already_set(
    sessions: sessionmaker[Session],
) -> None:
    task_id = enqueue(sessions)
    stop = asyncio.Event()
    stop.set()

    await runtime(sessions, TaskHandlerRegistry()).run(stop=stop)
    task, runs = load_task_and_runs(sessions, task_id)

    assert task.status == "pending"
    assert runs == []


def test_handler_registry_is_version_aware() -> None:
    registry = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        return TaskOutcome(status="success")

    registry.register("scan", 1, handler)

    assert registry.resolve("scan", 1) is handler
    assert registry.resolve("scan", 2) is None
    with pytest.raises(ValueError, match="already registered"):
        registry.register("scan", 1, handler)


def test_task_outcome_validates_state_specific_fields() -> None:
    with pytest.raises(ValueError, match="requires error_code"):
        TaskOutcome(status="retry")
    with pytest.raises(ValueError, match="requires blocked_reason"):
        TaskOutcome(status="waiting_credential")
    with pytest.raises(ValueError, match="only valid for retry"):
        TaskOutcome(status="success", retry_after=timedelta(seconds=1))
