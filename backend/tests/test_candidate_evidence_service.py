from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import Base
from app.repositories import UpdateOperationRepository
from app.services.candidate_evidence_service import (
    CandidateEvidenceError,
    CandidateEvidenceService,
    read_candidate_evidence,
)

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


def prepare_candidate(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    database = tmp_path / "mediasync.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('revision-head')")
        )
    with Session(engine) as session, session.begin():
        UpdateOperationRepository(session).create(
            operation_id=OPERATION_ID,
            source_version="0.2.0-rc.9",
            status="verifying",
            target_version=VERSION,
            target_digest=DIGEST,
        )
    engine.dispose()

    pending = tmp_path / "update" / "pending.json"
    pending.parent.mkdir(parents=True)
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
    environment = {
        "MEDIASYNC_CANDIDATE_TOKEN": CANDIDATE_TOKEN,
        "MEDIASYNC_IMAGE_REVISION": REVISION,
        "MEDIASYNC_IMAGE_DIGEST": DIGEST,
    }
    return pending, environment


def test_candidate_writes_strict_private_evidence_after_all_checks(
    tmp_path: Path,
) -> None:
    pending, environment = prepare_candidate(tmp_path)
    service = CandidateEvidenceService(
        data_directory=tmp_path,
        pending_path=pending,
        environment=environment,
        app_version=VERSION,
    )

    assert service.observe(COMPONENTS) is True

    evidence_path = tmp_path / "update" / "operations" / f"{OPERATION_ID}.candidate.json"
    evidence = read_candidate_evidence(
        evidence_path,
        expected_operation_id=OPERATION_ID,
        expected_candidate_token=CANDIDATE_TOKEN,
    )
    assert evidence.components == COMPONENTS
    assert evidence.alembic_revision == "revision-head"
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert "SECRET_KEY" not in evidence_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("components", "environment_key"),
    [
        ({**COMPONENTS, "worker": False}, None),
        (COMPONENTS, "MEDIASYNC_IMAGE_DIGEST"),
    ],
)
def test_candidate_rejects_unhealthy_or_forged_identity(
    tmp_path: Path,
    components: dict[str, bool],
    environment_key: str | None,
) -> None:
    pending, environment = prepare_candidate(tmp_path)
    if environment_key is not None:
        environment[environment_key] = f"sha256:{'f' * 64}"
    service = CandidateEvidenceService(
        data_directory=tmp_path,
        pending_path=pending,
        environment=environment,
        app_version=VERSION,
    )

    with pytest.raises(CandidateEvidenceError):
        service.observe(components)

    assert not (tmp_path / "update" / "operations" / f"{OPERATION_ID}.candidate.json").exists()
