from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Task, TaskRun
from app.models.base import utcnow
from app.repositories import (
    ActiveTaskRunExistsError,
    TaskRepository,
    TaskStateConflictError,
)
from app.task_engine import ALLOWED_TASK_TRANSITIONS, InvalidTaskTransitionError

ALLOWED_TRANSITION_CASES = [
    (source, target)
    for source, targets in ALLOWED_TASK_TRANSITIONS.items()
    for target in targets
]
FORBIDDEN_TRANSITION_CASES = [
    (source, target)
    for source in ALLOWED_TASK_TRANSITIONS
    for target in ALLOWED_TASK_TRANSITIONS
    if target not in ALLOWED_TASK_TRANSITIONS[source]
]


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.mark.parametrize(("source_status", "target_status"), ALLOWED_TRANSITION_CASES)
def test_every_allowed_task_transition_succeeds(
    db: Session,
    source_status: str,
    target_status: str,
) -> None:
    task = Task(type="scan", status=source_status)
    db.add(task)
    db.commit()

    transitioned = TaskRepository(db).transition(
        task.id,
        expected_status=source_status,
        target_status=target_status,
    )
    db.commit()

    assert transitioned.status == target_status


@pytest.mark.parametrize(("source_status", "target_status"), FORBIDDEN_TRANSITION_CASES)
def test_every_unlisted_task_transition_is_rejected(
    db: Session,
    source_status: str,
    target_status: str,
) -> None:
    task = Task(type="scan", status=source_status)
    db.add(task)
    db.commit()

    with pytest.raises(InvalidTaskTransitionError):
        TaskRepository(db).transition(
            task.id,
            expected_status=source_status,
            target_status=target_status,
        )

    assert task.status == source_status


def test_transition_rejects_expected_state_mismatch(db: Session) -> None:
    task = Task(type="scan", status="pending")
    db.add(task)
    db.commit()

    with pytest.raises(TaskStateConflictError) as exc_info:
        TaskRepository(db).transition(
            task.id,
            expected_status="retry",
            target_status="running",
        )

    assert exc_info.value.actual_status == "pending"
    assert task.status == "pending"


def test_cancel_requested_reentry_preserves_original_request_time(db: Session) -> None:
    repository = TaskRepository(db)
    task = Task(type="transfer", status="running")
    db.add(task)
    db.commit()
    requested_at = utcnow()

    repository.transition(
        task.id,
        expected_status="running",
        target_status="cancel_requested",
        occurred_at=requested_at,
    )
    repository.transition(
        task.id,
        expected_status="cancel_requested",
        target_status="cancel_requested",
        occurred_at=utcnow(),
    )

    assert task.status == "cancel_requested"
    assert task.cancel_requested_at == requested_at


def test_create_task_requires_pending_state(db: Session) -> None:
    repository = TaskRepository(db)

    with pytest.raises(ValueError, match="must start in pending"):
        repository.create_task(Task(type="scan", status="running"))


def test_runs_are_appended_with_monotonic_numbers(db: Session) -> None:
    repository = TaskRepository(db)
    task = repository.create_task(Task(type="transfer"))
    repository.transition(task.id, expected_status="pending", target_status="running")
    first = repository.create_run(task.id)
    repository.finish_run(
        task.id,
        first.id,
        expected_task_status="running",
        task_status="retry",
        run_status="failed",
        next_attempt_at=utcnow(),
    )
    repository.transition(task.id, expected_status="retry", target_status="running")
    second = repository.create_run(task.id)
    repository.finish_run(
        task.id,
        second.id,
        expected_task_status="running",
        task_status="success",
        run_status="success",
    )
    db.commit()

    persisted = repository.get(task.id, include_runs=True)
    assert persisted is not None
    assert persisted.status == "success"
    assert [run.run_number for run in persisted.runs] == [1, 2]
    assert [run.status for run in persisted.runs] == ["failed", "success"]


def test_create_run_rejects_a_second_active_run(db: Session) -> None:
    repository = TaskRepository(db)
    task = repository.create_task(Task(type="scan"))
    repository.transition(task.id, expected_status="pending", target_status="running")
    repository.create_run(task.id)

    with pytest.raises(ActiveTaskRunExistsError):
        repository.create_run(task.id)


def test_finish_run_requires_matching_task_and_run_outcomes(db: Session) -> None:
    repository = TaskRepository(db)
    task = repository.create_task(Task(type="scan"))
    repository.transition(task.id, expected_status="pending", target_status="running")
    task_run = repository.create_run(task.id)

    with pytest.raises(ValueError, match="requires run status"):
        repository.finish_run(
            task.id,
            task_run.id,
            expected_task_status="running",
            task_status="success",
            run_status="failed",
        )

    assert task.status == "running"
    assert task_run.status == "running"


def test_task_and_run_completion_roll_back_together(db: Session) -> None:
    repository = TaskRepository(db)
    task = repository.create_task(Task(type="scan"))
    repository.transition(task.id, expected_status="pending", target_status="running")
    task_run = repository.create_run(task.id)
    db.commit()

    with pytest.raises(IntegrityError):
        repository.finish_run(
            task.id,
            task_run.id,
            expected_task_status="running",
            task_status="success",
            run_status="success",
            duration_ms=-1,
        )
    db.rollback()

    persisted_task = db.get(Task, task.id)
    persisted_run = db.get(TaskRun, task_run.id)
    assert persisted_task is not None
    assert persisted_run is not None
    assert persisted_task.status == "running"
    assert persisted_task.completed_at is None
    assert persisted_run.status == "running"
    assert persisted_run.finished_at is None
