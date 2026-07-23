from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Base, Task, TaskRun
from app.repositories import (
    TaskOwnershipLostError,
    TaskOwnershipRequiredError,
    TaskRepository,
    TaskRunNotFoundError,
)

NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
NOW_SQLITE = NOW.replace(tzinfo=None)
LEASE = timedelta(seconds=90)


def repository(db: Session, *, clock: datetime = NOW) -> TaskRepository:
    return TaskRepository(db, clock=lambda: clock)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_claim_assigns_complete_ownership_and_creates_run(db: Session) -> None:
    task_repository = repository(db)
    task = task_repository.create_task(Task(type="scan"))

    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )

    assert claim is not None
    assert claim.task.id == task.id
    assert claim.task.status == "running"
    assert claim.task.locked_by == "worker-a"
    assert claim.task.lock_token == claim.lock_token
    assert claim.task.locked_at == NOW_SQLITE
    assert claim.task.lease_until == NOW_SQLITE + LEASE
    assert len(claim.lock_token) == 64
    assert claim.task_run.task_id == task.id
    assert claim.task_run.run_number == 1
    assert claim.task_run.worker_id == "worker-a"
    assert claim.task_run.lock_token == claim.lock_token
    assert claim.task_run.status == "running"
    assert claim.task_run.last_heartbeat_at == NOW


def test_claim_uses_priority_availability_and_stable_ordering(db: Session) -> None:
    tasks = [
        Task(
            type="scan",
            priority=10,
            created_at=NOW - timedelta(minutes=3),
            next_attempt_at=NOW - timedelta(minutes=1),
        ),
        Task(
            type="scan",
            priority=10,
            created_at=NOW - timedelta(minutes=2),
            next_attempt_at=NOW - timedelta(minutes=2),
        ),
        Task(
            type="scan",
            priority=5,
            created_at=NOW - timedelta(minutes=10),
        ),
    ]
    db.add_all(tasks)
    db.commit()

    claim = repository(db).claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )

    assert claim is not None
    assert claim.task.id == tasks[1].id


def test_claim_skips_retry_that_is_not_due(db: Session) -> None:
    task = Task(
        type="transfer",
        status="retry",
        next_attempt_at=NOW + timedelta(minutes=1),
    )
    db.add(task)
    db.commit()

    claim = repository(db).claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )

    assert claim is None
    assert task.status == "retry"


def test_claim_and_run_creation_roll_back_together(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_repository = repository(db)
    task = task_repository.create_task(Task(type="scan"))
    db.commit()

    def fail_run_creation(_task_run: TaskRun) -> TaskRun:
        raise RuntimeError("injected task run failure")

    monkeypatch.setattr(task_repository, "_append_task_run", fail_run_creation)

    with pytest.raises(RuntimeError, match="injected"):
        task_repository.claim_next(
            worker_id="worker-a",
            lease_duration=LEASE,
            claimed_at=NOW,
        )

    db.expire_all()
    persisted = db.get(Task, task.id)
    assert persisted is not None
    assert persisted.status == "pending"
    assert persisted.locked_by is None
    assert persisted.lock_token is None
    assert persisted.locked_at is None
    assert persisted.lease_until is None
    assert db.scalar(select(func.count(TaskRun.id))) == 0


def test_sequential_attempts_use_fresh_tokens_and_run_numbers(db: Session) -> None:
    task_repository = repository(db)
    task = task_repository.create_task(Task(type="transfer"))
    first = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert first is not None
    retry_at = NOW + timedelta(seconds=20)
    task_repository.finish_run(
        task.id,
        first.task_run.id,
        worker_id="worker-a",
        lock_token=first.lock_token,
        expected_task_status="running",
        task_status="retry",
        run_status="failed",
        finished_at=NOW + timedelta(seconds=10),
        next_attempt_at=retry_at,
    )

    second = task_repository.claim_next(
        worker_id="worker-b",
        lease_duration=LEASE,
        claimed_at=retry_at,
    )

    assert second is not None
    assert second.lock_token != first.lock_token
    assert second.task_run.run_number == 2
    assert second.task_run.worker_id == "worker-b"


def test_heartbeat_extends_current_lease_and_updates_run(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="scan"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None
    heartbeat_at = NOW + timedelta(seconds=30)

    task, task_run = task_repository.heartbeat(
        claim.task.id,
        worker_id="worker-a",
        lock_token=claim.lock_token,
        lease_duration=LEASE,
        heartbeat_at=heartbeat_at,
    )

    assert task.lease_until == heartbeat_at.replace(tzinfo=None) + LEASE
    assert task_run.last_heartbeat_at == heartbeat_at.replace(tzinfo=None)


def test_stale_or_expired_owner_cannot_heartbeat(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="scan"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None

    with pytest.raises(TaskOwnershipLostError):
        task_repository.heartbeat(
            claim.task.id,
            worker_id="worker-a",
            lock_token="stale-token",
            lease_duration=LEASE,
            heartbeat_at=NOW + timedelta(seconds=10),
        )
    with pytest.raises(TaskOwnershipLostError):
        task_repository.heartbeat(
            claim.task.id,
            worker_id="worker-a",
            lock_token=claim.lock_token,
            lease_duration=LEASE,
            heartbeat_at=NOW + LEASE,
        )

    db.expire_all()
    persisted = db.get(Task, claim.task.id)
    assert persisted is not None
    assert persisted.lease_until == NOW_SQLITE + LEASE


def test_fenced_finish_clears_ownership_and_terminalizes_run(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="scan"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None
    finished_at = NOW + timedelta(seconds=10)

    task, task_run = task_repository.finish_run(
        claim.task.id,
        claim.task_run.id,
        worker_id="worker-a",
        lock_token=claim.lock_token,
        expected_task_status="running",
        task_status="success",
        run_status="success",
        finished_at=finished_at,
    )

    assert task.status == "success"
    assert task.completed_at == finished_at.replace(tzinfo=None)
    assert task.locked_by is None
    assert task.lock_token is None
    assert task.locked_at is None
    assert task.lease_until is None
    assert task_run.status == "success"
    assert task_run.finished_at == finished_at.replace(tzinfo=None)


def test_stale_owner_cannot_finish_or_mutate_owned_state(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="transfer"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None

    with pytest.raises(TaskOwnershipLostError):
        task_repository.finish_run(
            claim.task.id,
            claim.task_run.id,
            worker_id="worker-a",
            lock_token="stale-token",
            expected_task_status="running",
            task_status="success",
            run_status="success",
            finished_at=NOW + timedelta(seconds=10),
        )

    db.expire_all()
    task = db.get(Task, claim.task.id)
    task_run = db.get(TaskRun, claim.task_run.id)
    assert task is not None
    assert task_run is not None
    assert task.status == "running"
    assert task.lock_token == claim.lock_token
    assert task_run.status == "running"


def test_expired_owner_cannot_finish(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="transfer"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None

    with pytest.raises(TaskOwnershipLostError):
        repository(db, clock=NOW + LEASE).finish_run(
            claim.task.id,
            claim.task_run.id,
            worker_id="worker-a",
            lock_token=claim.lock_token,
            expected_task_status="running",
            task_status="success",
            run_status="success",
            finished_at=NOW + timedelta(seconds=10),
        )

    db.expire_all()
    task = db.get(Task, claim.task.id)
    task_run = db.get(TaskRun, claim.task_run.id)
    assert task is not None
    assert task_run is not None
    assert task.status == "running"
    assert task_run.status == "running"


def test_cancel_request_preserves_ownership_until_fenced_finish(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="transfer"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None
    requested_at = NOW + timedelta(seconds=10)

    requested = task_repository.transition(
        claim.task.id,
        expected_status="running",
        target_status="cancel_requested",
        occurred_at=requested_at,
    )
    assert requested.lock_token == claim.lock_token
    assert requested.cancel_requested_at == requested_at.replace(tzinfo=None)

    task, task_run = task_repository.finish_run(
        claim.task.id,
        claim.task_run.id,
        worker_id="worker-a",
        lock_token=claim.lock_token,
        expected_task_status="cancel_requested",
        task_status="cancelled",
        run_status="cancelled",
        finished_at=NOW + timedelta(seconds=20),
    )

    assert task.status == "cancelled"
    assert task.lock_token is None
    assert task_run.status == "cancelled"


def test_missing_run_rolls_back_fenced_task_finish(db: Session) -> None:
    task_repository = repository(db)
    task_repository.create_task(Task(type="scan"))
    claim = task_repository.claim_next(
        worker_id="worker-a",
        lease_duration=LEASE,
        claimed_at=NOW,
    )
    assert claim is not None

    with pytest.raises(TaskRunNotFoundError):
        task_repository.finish_run(
            claim.task.id,
            claim.task_run.id + 100,
            worker_id="worker-a",
            lock_token=claim.lock_token,
            expected_task_status="running",
            task_status="success",
            run_status="success",
            finished_at=NOW + timedelta(seconds=10),
        )

    db.expire_all()
    task = db.get(Task, claim.task.id)
    assert task is not None
    assert task.status == "running"
    assert task.lock_token == claim.lock_token


def test_transition_cannot_bypass_claim_ownership(db: Session) -> None:
    task_repository = repository(db)
    task = task_repository.create_task(Task(type="scan"))

    with pytest.raises(TaskOwnershipRequiredError):
        task_repository.transition(
            task.id,
            expected_status="pending",
            target_status="running",
        )

    assert task.status == "pending"


def test_competing_sqlite_claims_do_not_acquire_same_task(tmp_path: Path) -> None:
    database_path = tmp_path / "claim-concurrency.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Task(type="scan"))
        session.commit()
    barrier = Barrier(2)

    def attempt(worker_id: str) -> str | None:
        with Session(engine) as session, session.begin():
            barrier.wait()
            claim = repository(session).claim_next(
                worker_id=worker_id,
                lease_duration=LEASE,
                claimed_at=NOW,
            )
            return claim.lock_token if claim is not None else None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ("worker-a", "worker-b")))

    assert sum(result is not None for result in results) == 1
    with Session(engine) as session:
        task = session.scalar(select(Task))
        runs = list(session.scalars(select(TaskRun)))
        assert task is not None
        assert task.status == "running"
        assert len(runs) == 1
        assert runs[0].lock_token == task.lock_token
    engine.dispose()


@pytest.mark.parametrize(
    "lease_duration",
    [timedelta(0), timedelta(seconds=-1)],
)
def test_claim_rejects_non_positive_lease(
    db: Session,
    lease_duration: timedelta,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        repository(db).claim_next(
            worker_id="worker-a",
            lease_duration=lease_duration,
            claimed_at=NOW,
        )
