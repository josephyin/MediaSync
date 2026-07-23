from collections.abc import Iterator
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Task, TaskRun
from app.models.base import utcnow
from app.repositories import (
    ActiveTaskRunExistsError,
    TaskClaim,
    TaskRepository,
    TaskStateConflictError,
)
from app.task_engine import (
    ALLOWED_TASK_TRANSITIONS,
    InvalidTaskTransitionError,
    validate_transition,
)

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
    source_status: str,
    target_status: str,
) -> None:
    validate_transition(source_status, target_status)


@pytest.mark.parametrize(("source_status", "target_status"), FORBIDDEN_TRANSITION_CASES)
def test_every_unlisted_task_transition_is_rejected(
    source_status: str,
    target_status: str,
) -> None:
    with pytest.raises(InvalidTaskTransitionError):
        validate_transition(source_status, target_status)


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
    assert task.cancel_requested_at == requested_at.replace(tzinfo=None)


def test_create_task_requires_pending_state(db: Session) -> None:
    repository = TaskRepository(db)

    with pytest.raises(ValueError, match="must start in pending"):
        repository.create_task(Task(type="scan", status="running"))


def test_runs_are_appended_with_monotonic_numbers(db: Session) -> None:
    repository = TaskRepository(db)
    task = repository.create_task(Task(type="transfer"))
    first_claim = _claim(repository)
    repository.finish_run(
        task.id,
        first_claim.task_run.id,
        worker_id="worker-1",
        lock_token=first_claim.lock_token,
        expected_task_status="running",
        task_status="retry",
        run_status="failed",
        next_attempt_at=utcnow(),
    )
    second_claim = _claim(repository, worker_id="worker-2")
    repository.finish_run(
        task.id,
        second_claim.task_run.id,
        worker_id="worker-2",
        lock_token=second_claim.lock_token,
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
    repository.create_task(Task(type="scan"))
    claim = _claim(repository)

    with pytest.raises(ActiveTaskRunExistsError):
        repository.create_run(
            claim.task.id,
            worker_id="worker-1",
            lock_token=claim.lock_token,
        )


def test_finish_run_requires_matching_task_and_run_outcomes(db: Session) -> None:
    repository = TaskRepository(db)
    repository.create_task(Task(type="scan"))
    claim = _claim(repository)

    with pytest.raises(ValueError, match="requires run status"):
        repository.finish_run(
            claim.task.id,
            claim.task_run.id,
            worker_id="worker-1",
            lock_token=claim.lock_token,
            expected_task_status="running",
            task_status="success",
            run_status="failed",
        )

    assert claim.task.status == "running"
    assert claim.task_run.status == "running"


def test_task_and_run_completion_roll_back_together(db: Session) -> None:
    repository = TaskRepository(db)
    repository.create_task(Task(type="scan"))
    claim = _claim(repository)
    db.commit()

    with pytest.raises(IntegrityError):
        repository.finish_run(
            claim.task.id,
            claim.task_run.id,
            worker_id="worker-1",
            lock_token=claim.lock_token,
            expected_task_status="running",
            task_status="success",
            run_status="success",
            duration_ms=-1,
        )
    db.rollback()

    persisted_task = db.get(Task, claim.task.id)
    persisted_run = db.get(TaskRun, claim.task_run.id)
    assert persisted_task is not None
    assert persisted_run is not None
    assert persisted_task.status == "running"
    assert persisted_task.completed_at is None
    assert persisted_run.status == "running"
    assert persisted_run.finished_at is None


def _claim(
    repository: TaskRepository,
    *,
    worker_id: str = "worker-1",
) -> TaskClaim:
    claim = repository.claim_next(
        worker_id=worker_id,
        lease_duration=timedelta(seconds=90),
    )
    assert claim is not None
    return claim
