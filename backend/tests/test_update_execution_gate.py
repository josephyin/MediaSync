from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, Task, UpdateOperation
from app.repositories import (
    ActiveUpdateOperationConflictError,
    TaskRepository,
    UpdateOperationRepository,
    UpdateOperationStateError,
)
from app.scheduler.runtime import SchedulerRuntime
from app.services.update_execution_gate import (
    UpdateExecutionGate,
    UpdateGateDecision,
)
from app.task_engine import TaskHandlerRegistry, TaskOutcome
from app.task_engine.worker import WorkerRuntime

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
DIGEST = f"sha256:{'a' * 64}"
REVISION = "b" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'update-gate.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def create_operation(session: Session, *, operation_id: str = OPERATION_ID):
    return UpdateOperationRepository(session).create(
        operation_id=operation_id,
        source_version="0.2.0-rc.9",
        target_version="0.3.0-rc.1",
        target_digest=DIGEST,
    )


def write_marker(path: Path, *, operation_id: str = OPERATION_ID) -> None:
    path.write_text(
        json.dumps(
            {
                "operation_id": operation_id,
                "target_version": "0.3.0-rc.1",
                "target_digest": DIGEST,
                "target_revision": REVISION,
                "candidate_token": CANDIDATE_TOKEN,
                "mode": "candidate_validation",
            }
        ),
        encoding="utf-8",
    )


def test_only_one_active_operation_and_terminal_record_cannot_be_reused(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        repository = UpdateOperationRepository(session)
        operation = create_operation(session)
        with pytest.raises(ActiveUpdateOperationConflictError):
            create_operation(
                session,
                operation_id="22345678-1234-4234-9234-123456789abc",
            )
        repository.finish(operation, status="cancelled")
        with pytest.raises(UpdateOperationStateError):
            repository.finish(operation, status="success")
        replacement = create_operation(
            session,
            operation_id="32345678-1234-4234-9234-123456789abc",
        )
        assert replacement.active_slot == "global"
        repository.transition_active(replacement, status="handoff")
        with pytest.raises(UpdateOperationStateError):
            repository.transition_active(replacement, status="available")


def test_verified_target_can_only_be_recorded_once_while_pulling(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        repository = UpdateOperationRepository(session)
        operation = repository.create(
            source_version="0.2.0-rc.9",
            status="available",
        )
        repository.transition_active(operation, status="pulling")

        repository.set_verified_target(
            operation,
            target_version="v0.3.0-rc.1",
            target_digest=DIGEST,
        )

        assert operation.target_version == "v0.3.0-rc.1"
        assert operation.target_digest == DIGEST
        with pytest.raises(UpdateOperationStateError):
            repository.set_verified_target(
                operation,
                target_version="v0.3.0-rc.1",
                target_digest=f"sha256:{'b' * 64}",
            )


def test_verified_target_cannot_be_recorded_before_pulling(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        repository = UpdateOperationRepository(session)
        operation = repository.create(
            source_version="0.2.0-rc.9",
            status="available",
        )

        with pytest.raises(UpdateOperationStateError):
            repository.set_verified_target(
                operation,
                target_version="v0.3.0-rc.1",
                target_digest=DIGEST,
            )


def test_terminal_operation_history_is_database_protected(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        operation = create_operation(session)
        operation_id = operation.id
        UpdateOperationRepository(session).finish(operation, status="cancelled")

    with sessions() as session:
        with pytest.raises(IntegrityError):
            session.execute(
                update(UpdateOperation)
                .where(UpdateOperation.id == operation_id)
                .values(status="failed")
            )
            session.commit()
        session.rollback()
        with pytest.raises(IntegrityError):
            session.execute(
                delete(UpdateOperation).where(UpdateOperation.id == operation_id)
            )
            session.commit()


def test_gate_modes_cover_normal_draining_candidate_and_invalid_marker(
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    marker = tmp_path / "pending.json"
    gate = UpdateExecutionGate(pending_path=str(marker))
    with sessions() as session:
        assert gate.evaluate(session).mode == "normal"

    with sessions() as session, session.begin():
        create_operation(session)
    with sessions() as session:
        assert gate.evaluate(session).mode == "draining"

    write_marker(marker)
    with sessions() as session:
        candidate = gate.evaluate(session)
        assert candidate.blocked is True
        assert candidate.mode == "candidate_validation"
        assert candidate.operation_id == OPERATION_ID

    write_marker(
        marker,
        operation_id="42345678-1234-4234-9234-123456789abc",
    )
    with sessions() as session:
        assert gate.evaluate(session).mode == "candidate_invalid"

    marker.write_text("{bad-json", encoding="utf-8")
    with sessions() as session:
        invalid = gate.evaluate(session)
        assert invalid.blocked is True
        assert invalid.mode == "candidate_invalid"


def test_scheduler_does_not_enqueue_during_drain(
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with sessions() as session, session.begin():
        create_operation(session)
    runtime = SchedulerRuntime(
        session_factory=sessions,
        batch_size=10,
        update_gate=UpdateExecutionGate(
            pending_path=str(tmp_path / "missing.json")
        ),
    )

    result = runtime.run_once()

    assert result.gated is True
    assert result.enqueued_count == 0


async def test_worker_pauses_before_claim_but_running_handler_can_finish(
    sessions: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    with sessions() as session, session.begin():
        task = TaskRepository(session).create_task(
            Task(type="test", payload_version=1, payload={})
        )
        task_id = task.id
        create_operation(session)

    handlers = TaskHandlerRegistry()

    async def handler(_context) -> TaskOutcome:
        with sessions() as session, session.begin():
            create_operation(
                session,
                operation_id="52345678-1234-4234-9234-123456789abc",
            )
        return TaskOutcome(status="success", summary="done")

    handlers.register("test", 1, handler)
    runtime = WorkerRuntime(
        session_factory=sessions,
        handlers=handlers,
        worker_id="worker-a",
        update_gate=UpdateExecutionGate(
            pending_path=str(tmp_path / "missing.json")
        ),
    )

    paused = await runtime.run_once()
    with sessions() as session, session.begin():
        operation = UpdateOperationRepository(session).get_active()
        assert operation is not None
        UpdateOperationRepository(session).finish(operation, status="cancelled")
    completed = await runtime.run_once()

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "success"
    assert paused.status == "paused"
    assert completed.status == "completed"


def test_candidate_validation_http_guard_keeps_only_safe_api(monkeypatch) -> None:
    from app import main

    class CandidateGate:
        @staticmethod
        def pending_marker_present() -> bool:
            return True

        @staticmethod
        def evaluate(_session: Session) -> UpdateGateDecision:
            return UpdateGateDecision(
                blocked=True,
                mode="candidate_validation",
                operation_id=OPERATION_ID,
            )

    monkeypatch.setattr(main, "build_update_execution_gate", CandidateGate)

    with TestClient(main.app) as client:
        assert client.get("/api/v1/system/health").status_code == 200
        assert client.get("/api/v1/auth/status").status_code == 200
        assert client.get("/api/v1/system/update").status_code == 401
        blocked = client.get("/api/v1/dashboard/summary")
        assert blocked.status_code == 423
        assert blocked.json()["runtime_mode"] == "candidate_validation"
