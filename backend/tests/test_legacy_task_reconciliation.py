from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Task, TaskRun
from app.repositories import (
    LegacyTaskRunConflictError,
    TaskRepository,
)
from app.repositories.tasks import (
    LEGACY_CUTOVER_ERROR_CODE,
    LEGACY_CUTOVER_ERROR_MESSAGE,
)
from app.services.legacy_task_reconciliation import reconcile_legacy_tasks
from app.task_engine.worker import ExponentialBackoff

NOW = datetime(2026, 7, 24, 16, 0, tzinfo=UTC)
BACKOFF = ExponentialBackoff(
    base_delay=timedelta(seconds=30),
    max_delay=timedelta(minutes=15),
)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "legacy-task-reconciliation.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def add_task(
    session: Session,
    *,
    status: str,
    attempt_count: int = 0,
    retry_count: int = 0,
    max_retries: int = 3,
    owned: bool = False,
) -> Task:
    ownership = (
        {
            "locked_by": "worker-v2",
            "lock_token": "v2-token",
            "locked_at": NOW,
            "lease_until": NOW + timedelta(seconds=60),
        }
        if owned
        else {}
    )
    task = Task(
        type="transfer",
        trigger_type="scheduled",
        status=status,
        payload_version=1,
        payload={},
        attempt_count=attempt_count,
        retry_count=retry_count,
        max_retries=max_retries,
        started_at=NOW - timedelta(seconds=10) if status == "running" else None,
        **ownership,
    )
    session.add(task)
    session.flush()
    return task


def add_active_legacy_run(session: Session, task: Task) -> TaskRun:
    task_run = TaskRun(
        task_id=task.id,
        run_number=max(task.attempt_count, 1),
        worker_id="legacy-v0.1",
        lock_token=None,
        status="running",
        started_at=task.started_at or NOW,
        metrics={"schema_version": 1, "migrated_from": "v0.1"},
    )
    session.add(task_run)
    session.flush()
    return task_run


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "retry",
        "waiting_credential",
        "cancel_requested",
        "success",
        "failed",
        "cancelled",
    ],
)
def test_non_running_states_are_preserved(
    sessions: sessionmaker[Session],
    status: str,
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status=status)
        task_id = task.id

    with sessions() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        persisted = session.get(Task, task_id)
        assert persisted is not None
        assert persisted.status == status
        assert result.inspected_count == 1
        assert result.preserved_by_status == {status: 1}
        assert session.scalar(select(TaskRun).where(TaskRun.task_id == task_id)) is None


def test_complete_v2_ownership_is_left_for_lease_recovery(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status="running", owned=True)
        run = TaskRun(
            task_id=task.id,
            run_number=1,
            worker_id=task.locked_by,
            lock_token=task.lock_token,
            status="running",
            started_at=NOW,
        )
        session.add(run)
        session.flush()
        task_id = task.id
        run_id = run.id

    with sessions() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        task = session.get(Task, task_id)
        run = session.get(TaskRun, run_id)
        assert task is not None
        assert run is not None
        assert task.status == "running"
        assert task.lock_token == "v2-token"
        assert run.status == "running"
        assert result.preserved_by_status == {"running_v2_owned": 1}


def test_legacy_orphan_moves_to_retry_and_finalizes_migrated_run(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(
            session,
            status="running",
            attempt_count=1,
            max_retries=2,
        )
        run = add_active_legacy_run(session, task)
        task_id = task.id
        run_id = run.id

    with sessions() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        task = session.get(Task, task_id)
        run = session.get(TaskRun, run_id)
        assert task is not None
        assert run is not None
        assert task.status == "retry"
        assert task.retry_count == 1
        assert task.next_attempt_at == (NOW + timedelta(seconds=30)).replace(tzinfo=None)
        assert task.last_error_code == LEGACY_CUTOVER_ERROR_CODE
        assert task.last_error_message == LEGACY_CUTOVER_ERROR_MESSAGE
        assert task.message is None
        assert task.started_at == (NOW - timedelta(seconds=10)).replace(tzinfo=None)
        assert run.status == "lost"
        assert run.error_code == LEGACY_CUTOVER_ERROR_CODE
        assert run.finished_at == NOW.replace(tzinfo=None)
        assert result.orphan_retry_count == 1
        assert result.run_finalized_count == 1


def test_exhausted_legacy_orphan_moves_to_failed_without_decreasing_history(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(
            session,
            status="running",
            attempt_count=3,
            retry_count=2,
            max_retries=2,
        )
        add_active_legacy_run(session, task)
        task_id = task.id

    with sessions() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "failed"
        assert task.retry_count == 3
        assert task.next_attempt_at is None
        assert task.completed_at == NOW.replace(tzinfo=None)
        assert result.orphan_failed_count == 1


def test_orphan_without_active_run_appends_synthetic_lost_run(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status="running", max_retries=2)
        task_id = task.id

    with sessions() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        assert run is not None
        assert run.run_number == 1
        assert run.worker_id == "legacy-cutover"
        assert run.status == "lost"
        assert run.error_code == LEGACY_CUTOVER_ERROR_CODE
        assert run.metrics == {
            "schema_version": 1,
            "reconciled_from": "legacy",
        }
        assert result.run_appended_count == 1


def test_reconciliation_is_idempotent(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status="running", max_retries=2)
        add_active_legacy_run(session, task)
        task_id = task.id

    with sessions() as session, session.begin():
        first = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )
    with sessions() as session, session.begin():
        second = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW + timedelta(minutes=1),
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        runs = list(
            session.scalars(
                select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.run_number)
            )
        )
        assert len(runs) == 1
        assert runs[0].status == "lost"
        assert first.run_finalized_count == 1
        assert second.orphan_retry_count == 0
        assert second.preserved_by_status == {"retry": 1}


def test_existing_equivalent_lost_run_is_not_duplicated(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status="running", max_retries=2)
        run = TaskRun(
            task_id=task.id,
            run_number=1,
            worker_id="legacy-cutover",
            status="lost",
            started_at=NOW - timedelta(seconds=10),
            finished_at=NOW - timedelta(seconds=5),
            error_code=LEGACY_CUTOVER_ERROR_CODE,
            error_message=LEGACY_CUTOVER_ERROR_MESSAGE,
            metrics={},
        )
        session.add(run)
        task_id = task.id

    with sessions() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=NOW,
            retry_backoff=BACKOFF,
        )

    with sessions() as session:
        runs = list(session.scalars(select(TaskRun).where(TaskRun.task_id == task_id)))
        assert len(runs) == 1
        assert result.run_preserved_count == 1


def test_conflicting_active_runs_roll_back_task_reconciliation(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status="running", max_retries=2)
        first = add_active_legacy_run(session, task)
        session.add(
            TaskRun(
                task_id=task.id,
                run_number=first.run_number + 1,
                worker_id="legacy-v0.1-duplicate",
                status="running",
                started_at=NOW,
            )
        )
        task_id = task.id

    with pytest.raises(LegacyTaskRunConflictError):
        with sessions() as session, session.begin():
            reconcile_legacy_tasks(
                session,
                reconciled_at=NOW,
                retry_backoff=BACKOFF,
            )

    with sessions() as session:
        task = session.get(Task, task_id)
        runs = list(session.scalars(select(TaskRun).where(TaskRun.task_id == task_id)))
        assert task is not None
        assert task.status == "running"
        assert [run.status for run in runs] == ["running", "running"]


def test_repository_reconciliation_returns_none_for_terminal_task(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = add_task(session, status="failed")
        task_id = task.id

    with sessions() as session, session.begin():
        result = TaskRepository(session).reconcile_legacy_orphan(
            task_id,
            next_attempt_at=NOW + timedelta(seconds=30),
            reconciled_at=NOW,
        )

        assert result is None
