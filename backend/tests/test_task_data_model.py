from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Task, TaskRun
from app.models.base import utcnow
from app.repositories import TaskRunRepository


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_task_idempotency_key_is_unique(db: Session) -> None:
    db.add_all(
        [
            Task(type="scan", idempotency_key="scan:42:2026-07-23T10:00:00Z"),
            Task(type="scan", idempotency_key="scan:42:2026-07-23T10:00:00Z"),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_task_payload_version_defaults_to_one(db: Session) -> None:
    task = Task(type="scan", payload={"subscription_id": 42})
    db.add(task)
    db.commit()

    assert task.payload_version == 1
    assert task.payload == {"subscription_id": 42}

    task.payload_version = 2
    with pytest.raises(IntegrityError, match="task payload and version are immutable"):
        db.commit()


def test_task_idempotency_key_is_immutable_once_assigned(db: Session) -> None:
    task = Task(type="scan", idempotency_key="scan:42:scheduled")
    db.add(task)
    db.commit()

    task.idempotency_key = "scan:42:rewritten"
    with pytest.raises(IntegrityError, match="task idempotency key is immutable"):
        db.commit()


def test_task_ownership_fields_must_be_complete(db: Session) -> None:
    db.add(Task(type="scan", locked_by="worker-a"))

    with pytest.raises(IntegrityError):
        db.commit()


def test_task_run_number_is_unique_per_task(db: Session) -> None:
    task = Task(type="scan")
    db.add(task)
    db.flush()
    started_at = utcnow()
    db.add_all(
        [
            TaskRun(task_id=task.id, run_number=1, status="running", started_at=started_at),
            TaskRun(task_id=task.id, run_number=1, status="running", started_at=started_at),
        ]
    )

    with pytest.raises(IntegrityError):
        db.commit()


def test_active_task_run_identity_is_immutable(db: Session) -> None:
    task = Task(type="scan")
    db.add(task)
    db.flush()
    task_run = TaskRun(
        task_id=task.id,
        run_number=1,
        status="running",
        started_at=utcnow(),
    )
    db.add(task_run)
    db.commit()

    task_run.run_number = 2
    with pytest.raises(IntegrityError, match="task run identity is immutable"):
        db.commit()


def test_terminal_task_run_cannot_be_changed_or_deleted(db: Session) -> None:
    task = Task(type="scan")
    db.add(task)
    db.flush()
    repository = TaskRunRepository(db)
    task_run = repository.append(
        TaskRun(task_id=task.id, run_number=1, status="running", started_at=utcnow())
    )
    repository.finalize(
        task_run.id,
        status="success",
        finished_at=utcnow(),
        result_summary="scan completed",
        metrics={"items_seen": 10},
    )
    db.commit()

    task_run.result_summary = "rewritten result"
    with pytest.raises(IntegrityError, match="terminal task run is immutable"):
        db.commit()
    db.rollback()

    persisted = db.get(TaskRun, task_run.id)
    assert persisted is not None
    db.delete(persisted)
    with pytest.raises(IntegrityError, match="task run history cannot be deleted"):
        db.commit()


def test_later_attempt_appends_a_new_task_run(db: Session) -> None:
    task = Task(type="transfer")
    db.add(task)
    db.flush()
    repository = TaskRunRepository(db)
    first = repository.append(
        TaskRun(task_id=task.id, run_number=1, status="running", started_at=utcnow())
    )
    repository.finalize(first.id, status="failed", finished_at=utcnow())
    second = repository.append(
        TaskRun(task_id=task.id, run_number=2, status="running", started_at=utcnow())
    )
    db.commit()

    assert first.status == "failed"
    assert second.status == "running"
    assert [run.run_number for run in task.runs] == [1, 2]
