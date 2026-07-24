from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.models import Base, Task, TaskRun
from app.repositories import TaskRepository, TaskRunNotFoundError

CLAIMED_AT = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
LEASE_DURATION = timedelta(seconds=90)
RECOVERED_AT = CLAIMED_AT + timedelta(seconds=120)
RETRY_AT = RECOVERED_AT + timedelta(seconds=30)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def claim_task(
    db: Session,
    *,
    max_retries: int = 3,
) -> tuple[TaskRepository, Task, int, str]:
    repository = TaskRepository(db, clock=lambda: CLAIMED_AT)
    task = repository.create_task(Task(type="transfer", max_retries=max_retries))
    claim = repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE_DURATION,
    )
    assert claim is not None
    return repository, task, claim.task_run.id, claim.lock_token


def expired_lease(repository: TaskRepository):
    leases = repository.list_expired_leases(expired_at=RECOVERED_AT)
    assert len(leases) == 1
    return leases[0]


def test_expired_lease_discovery_is_bounded_and_ordered(db: Session) -> None:
    repository = TaskRepository(db, clock=lambda: CLAIMED_AT)
    first = repository.create_task(Task(type="scan", priority=1))
    first_claim = repository.claim_next(
        worker_id="worker-a",
        lease_duration=timedelta(seconds=30),
        claimed_at=CLAIMED_AT,
    )
    second = repository.create_task(Task(type="scan", priority=10))
    second_claim = repository.claim_next(
        worker_id="worker-b",
        lease_duration=timedelta(seconds=60),
        claimed_at=CLAIMED_AT,
    )
    repository.create_task(Task(type="scan"))
    assert first_claim is not None
    assert second_claim is not None

    leases = repository.list_expired_leases(
        limit=2,
        expired_at=CLAIMED_AT + timedelta(seconds=60),
    )

    assert [lease.task_id for lease in leases] == [first.id, second.id]
    assert all(lease.lock_token for lease in leases)


@pytest.mark.parametrize("limit", [0, 1001])
def test_expired_lease_discovery_validates_limit(db: Session, limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 1000"):
        TaskRepository(db).list_expired_leases(limit=limit)


def test_running_expired_lease_moves_to_retry_and_marks_run_lost(db: Session) -> None:
    repository, task, task_run_id, lock_token = claim_task(db)
    lease = expired_lease(repository)

    recovered = repository.recover_expired_lease(
        lease,
        next_attempt_at=RETRY_AT,
        recovered_at=RECOVERED_AT,
    )

    assert recovered is not None
    assert recovered.previous_lock_token == lock_token
    assert recovered.task.id == task.id
    assert recovered.task.status == "retry"
    assert recovered.task.retry_count == 1
    assert recovered.task.next_attempt_at == RETRY_AT.replace(tzinfo=None)
    assert recovered.task.locked_by is None
    assert recovered.task.lock_token is None
    assert recovered.task.locked_at is None
    assert recovered.task.lease_until is None
    assert recovered.task.last_error_code == "WORKER_LEASE_EXPIRED"
    assert recovered.task_run.id == task_run_id
    assert recovered.task_run.status == "lost"
    assert recovered.task_run.error_code == "WORKER_LEASE_EXPIRED"
    assert recovered.task_run.finished_at == RECOVERED_AT.replace(tzinfo=None)


def test_recovery_is_idempotent_for_the_same_expired_token(db: Session) -> None:
    repository, task, _task_run_id, _lock_token = claim_task(db)
    lease = expired_lease(repository)

    first = repository.recover_expired_lease(
        lease,
        next_attempt_at=RETRY_AT,
        recovered_at=RECOVERED_AT,
    )
    second = repository.recover_expired_lease(
        lease,
        next_attempt_at=RETRY_AT,
        recovered_at=RECOVERED_AT,
    )

    assert first is not None
    assert second is None
    db.expire_all()
    persisted = db.get(Task, task.id)
    assert persisted is not None
    assert persisted.retry_count == 1


def test_stale_recovery_token_does_not_mutate_task_or_run(db: Session) -> None:
    repository, task, task_run_id, lock_token = claim_task(db)
    stale = replace(expired_lease(repository), lock_token="stale-token")

    recovered = repository.recover_expired_lease(
        stale,
        next_attempt_at=RETRY_AT,
        recovered_at=RECOVERED_AT,
    )

    assert recovered is None
    db.expire_all()
    persisted_task = db.get(Task, task.id)
    persisted_run = db.get(TaskRun, task_run_id)
    assert persisted_task is not None
    assert persisted_run is not None
    assert persisted_task.status == "running"
    assert persisted_task.lock_token == lock_token
    assert persisted_task.retry_count == 0
    assert persisted_run.status == "running"


def test_lease_extension_wins_over_prefetched_recovery(db: Session) -> None:
    repository, task, task_run_id, _lock_token = claim_task(db)
    lease = expired_lease(repository)
    db.execute(
        update(Task)
        .where(Task.id == task.id)
        .values(lease_until=RECOVERED_AT + timedelta(seconds=60))
    )
    db.flush()

    recovered = repository.recover_expired_lease(
        lease,
        next_attempt_at=RETRY_AT,
        recovered_at=RECOVERED_AT,
    )

    assert recovered is None
    db.expire_all()
    persisted_task = db.get(Task, task.id)
    persisted_run = db.get(TaskRun, task_run_id)
    assert persisted_task is not None
    assert persisted_run is not None
    assert persisted_task.status == "running"
    assert persisted_task.retry_count == 0
    assert persisted_run.status == "running"


def test_retry_budget_exhaustion_moves_task_to_failed(db: Session) -> None:
    repository, task, _task_run_id, _lock_token = claim_task(db, max_retries=0)
    lease = expired_lease(repository)

    recovered = repository.recover_expired_lease(
        lease,
        recovered_at=RECOVERED_AT,
    )

    assert recovered is not None
    assert recovered.task.id == task.id
    assert recovered.task.status == "failed"
    assert recovered.task.retry_count == 1
    assert recovered.task.completed_at == RECOVERED_AT.replace(tzinfo=None)
    assert recovered.task_run.status == "lost"


def test_retryable_recovery_requires_next_attempt_time(db: Session) -> None:
    repository, task, task_run_id, lock_token = claim_task(db)
    lease = expired_lease(repository)

    with pytest.raises(ValueError, match="next_attempt_at is required"):
        repository.recover_expired_lease(lease, recovered_at=RECOVERED_AT)

    db.expire_all()
    persisted_task = db.get(Task, task.id)
    persisted_run = db.get(TaskRun, task_run_id)
    assert persisted_task is not None
    assert persisted_run is not None
    assert persisted_task.status == "running"
    assert persisted_task.lock_token == lock_token
    assert persisted_run.status == "running"


def test_cancel_requested_recovery_is_reclaimable_for_reconciliation(db: Session) -> None:
    repository, task, task_run_id, first_token = claim_task(db)
    requested_at = CLAIMED_AT + timedelta(seconds=10)
    repository.transition(
        task.id,
        expected_status="running",
        target_status="cancel_requested",
        occurred_at=requested_at,
    )
    lease = expired_lease(repository)

    recovered = repository.recover_expired_lease(
        lease,
        recovered_at=RECOVERED_AT,
    )
    second_claim = repository.claim_next(
        worker_id="worker-b",
        lease_duration=LEASE_DURATION,
        claimed_at=RECOVERED_AT + timedelta(seconds=1),
    )

    assert recovered is not None
    assert recovered.task_run.id == task_run_id
    assert recovered.task_run.status == "lost"
    assert recovered.task.retry_count == 1
    assert recovered.task.cancel_requested_at == requested_at.replace(tzinfo=None)
    assert second_claim is not None
    assert second_claim.task.id == task.id
    assert second_claim.task.status == "cancel_requested"
    assert second_claim.lock_token != first_token
    assert second_claim.task_run.run_number == 2


def test_task_and_run_recovery_roll_back_together(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, task, task_run_id, lock_token = claim_task(db)
    lease = expired_lease(repository)

    monkeypatch.setattr(repository, "_mark_task_run_lost", lambda **_kwargs: None)

    with pytest.raises(TaskRunNotFoundError):
        repository.recover_expired_lease(
            lease,
            next_attempt_at=RETRY_AT,
            recovered_at=RECOVERED_AT,
        )

    db.expire_all()
    persisted_task = db.get(Task, task.id)
    persisted_run = db.get(TaskRun, task_run_id)
    assert persisted_task is not None
    assert persisted_run is not None
    assert persisted_task.status == "running"
    assert persisted_task.lock_token == lock_token
    assert persisted_task.retry_count == 0
    assert persisted_run.status == "running"


def test_recovered_retry_creates_new_run_only_when_claimed_again(db: Session) -> None:
    repository, task, _task_run_id, _lock_token = claim_task(db)
    lease = expired_lease(repository)
    repository.recover_expired_lease(
        lease,
        next_attempt_at=RETRY_AT,
        recovered_at=RECOVERED_AT,
    )

    before_claim = list(
        db.scalars(select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.id))
    )
    second_claim = repository.claim_next(
        worker_id="worker-b",
        lease_duration=LEASE_DURATION,
        claimed_at=RETRY_AT,
    )

    assert [run.status for run in before_claim] == ["lost"]
    assert second_claim is not None
    assert second_claim.task_run.run_number == 2
