from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.repositories import UpdateOperationRepository
from app.services.candidate_evidence_service import CandidateEvidenceService
from app.services.update_execution_gate import UpdateExecutionGate
from app.update_reconcile import UpdateReconciliationError, UpdateTerminalReconciler

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
VERSION = "0.3.0-rc.1"
DIGEST = f"sha256:{'a' * 64}"
REVISION = "b" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"
COMPONENTS = {
    "launcher": True,
    "nginx": True,
    "api": True,
    "scheduler": True,
    "worker": True,
}


def prepare_runtime(
    tmp_path: Path,
    *,
    operation_status: str,
    result_status: str,
) -> tuple[sessionmaker[Session], Path, Path]:
    database = tmp_path / "mediasync.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('revision-head')"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session, session.begin():
        UpdateOperationRepository(session).create(
            operation_id=OPERATION_ID,
            source_version="0.2.0-rc.9",
            status=operation_status,
            target_version=VERSION,
            target_digest=DIGEST,
        )

    update_directory = tmp_path / "update"
    operations_directory = update_directory / "operations"
    operations_directory.mkdir(parents=True)
    pending = update_directory / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "operation_id": OPERATION_ID,
                "target_version": VERSION,
                "target_digest": DIGEST,
                "target_revision": REVISION,
                "candidate_token": CANDIDATE_TOKEN,
                "mode": "candidate_validation",
            }
        ),
        encoding="utf-8",
    )
    (operations_directory / f"{OPERATION_ID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "operation_id": OPERATION_ID,
                "sequence": 4,
                "status": result_status,
                "updated_at": datetime.now(UTC).isoformat(),
                "error_code": None,
                "public_error_message": None,
            }
        ),
        encoding="utf-8",
    )
    (operations_directory / f"{OPERATION_ID}.handoff.json").write_text("{}", encoding="utf-8")
    return factory, pending, operations_directory


def write_candidate_evidence(tmp_path: Path, pending: Path) -> None:
    CandidateEvidenceService(
        data_directory=tmp_path,
        pending_path=pending,
        environment={
            "MEDIASYNC_CANDIDATE_TOKEN": CANDIDATE_TOKEN,
            "MEDIASYNC_IMAGE_REVISION": REVISION,
            "MEDIASYNC_IMAGE_DIGEST": DIGEST,
        },
        app_version=VERSION,
    ).observe(COMPONENTS)


def test_commit_requested_result_commits_terminal_state_then_cleans_runtime_markers(
    tmp_path: Path,
) -> None:
    factory, pending, operations = prepare_runtime(
        tmp_path,
        operation_status="verifying",
        result_status="commit_requested",
    )
    write_candidate_evidence(tmp_path, pending)

    changed = UpdateTerminalReconciler(
        session_factory=factory,
        data_directory=tmp_path,
        pending_path=pending,
        allow_active_commit=True,
    ).reconcile()

    with factory() as session:
        operation = UpdateOperationRepository(session).get_by_operation_id(OPERATION_ID)
        assert operation is not None
        assert operation.status == "success"
        assert operation.active_slot is None
    assert changed is True
    assert not pending.exists()
    assert not (operations / f"{OPERATION_ID}.candidate.json").exists()
    assert not (operations / f"{OPERATION_ID}.handoff.json").exists()
    assert (operations / f"{OPERATION_ID}.json").exists()


def test_startup_reconciliation_defers_active_commit_until_current_health_observation(
    tmp_path: Path,
) -> None:
    factory, pending, _operations = prepare_runtime(
        tmp_path,
        operation_status="verifying",
        result_status="commit_requested",
    )
    write_candidate_evidence(tmp_path, pending)

    assert not UpdateTerminalReconciler(
        session_factory=factory,
        data_directory=tmp_path,
        pending_path=pending,
    ).reconcile()

    with factory() as session:
        operation = UpdateOperationRepository(session).get_active()
        assert operation is not None and operation.status == "verifying"
    assert pending.exists()


def test_cleanup_failure_keeps_gate_closed_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    factory, pending, operations = prepare_runtime(
        tmp_path,
        operation_status="verifying",
        result_status="commit_requested",
    )
    write_candidate_evidence(tmp_path, pending)

    def fail_first_cleanup(path: Path) -> None:
        if path.name.endswith("candidate.json"):
            raise OSError("disk busy")
        path.unlink(missing_ok=True)

    with pytest.raises(UpdateReconciliationError):
        UpdateTerminalReconciler(
            session_factory=factory,
            data_directory=tmp_path,
            pending_path=pending,
            unlink=fail_first_cleanup,
            allow_active_commit=True,
        ).reconcile()

    with factory() as session:
        operation = UpdateOperationRepository(session).get_by_operation_id(OPERATION_ID)
        assert operation is not None and operation.status == "success"
        assert UpdateExecutionGate(pending_path=str(pending)).evaluate(session).blocked

    assert UpdateTerminalReconciler(
        session_factory=factory,
        data_directory=tmp_path,
        pending_path=pending,
    ).reconcile()
    assert not pending.exists()
    assert not (operations / f"{OPERATION_ID}.candidate.json").exists()


def test_rolled_back_result_releases_gate_without_candidate_evidence(
    tmp_path: Path,
) -> None:
    factory, pending, _operations = prepare_runtime(
        tmp_path,
        operation_status="rolling_back",
        result_status="rolled_back",
    )

    assert UpdateTerminalReconciler(
        session_factory=factory,
        data_directory=tmp_path,
        pending_path=pending,
    ).reconcile()

    with factory() as session:
        operation = UpdateOperationRepository(session).get_by_operation_id(OPERATION_ID)
        assert operation is not None and operation.status == "rolled_back"


def test_rollback_failed_preserves_active_operation_and_manual_gate(
    tmp_path: Path,
) -> None:
    factory, pending, operations = prepare_runtime(
        tmp_path,
        operation_status="rolling_back",
        result_status="rollback_failed",
    )

    assert not UpdateTerminalReconciler(
        session_factory=factory,
        data_directory=tmp_path,
        pending_path=pending,
    ).reconcile()

    with factory() as session:
        operation = UpdateOperationRepository(session).get_active()
        assert operation is not None and operation.status == "rolling_back"
    assert pending.exists()
    assert (operations / f"{OPERATION_ID}.handoff.json").exists()


def test_forged_result_operation_id_is_rejected_without_state_change(
    tmp_path: Path,
) -> None:
    factory, pending, operations = prepare_runtime(
        tmp_path,
        operation_status="rolling_back",
        result_status="rolled_back",
    )
    result_path = operations / f"{OPERATION_ID}.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    payload["operation_id"] = "22345678-1234-4234-9234-123456789abc"
    result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UpdateReconciliationError):
        UpdateTerminalReconciler(
            session_factory=factory,
            data_directory=tmp_path,
            pending_path=pending,
        ).reconcile()

    with factory() as session:
        operation = UpdateOperationRepository(session).get_active()
        assert operation is not None and operation.status == "rolling_back"
    assert pending.exists()
