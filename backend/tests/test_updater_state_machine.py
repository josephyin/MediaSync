from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.services.candidate_evidence_service import CandidateEvidence
from app.services.docker_capability_service import APPLIANCE_COMMAND, OFFICIAL_SOURCE
from app.services.update_snapshot_service import (
    HandoffDocument,
    write_private_json,
)
from app.services.updater_candidate_service import UpdaterCandidateService
from app.services.updater_handoff_service import (
    CandidateContainerTemplate,
    SafeMount,
    UpdaterHandoffIntent,
    UpdaterHandoffStore,
)
from app.services.updater_state_machine import (
    ApplianceCommitWaiter,
    CandidateHealthVerifier,
    UpdaterStateMachine,
    UpdaterStateMachineError,
)

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
OLD_CONTAINER_ID = "a" * 64
CANDIDATE_ID = "b" * 64
SOURCE_IMAGE_ID = f"sha256:{'c' * 64}"
TARGET_IMAGE_ID = f"sha256:{'d' * 64}"
TARGET_DIGEST = f"sha256:{'e' * 64}"
TARGET_REVISION = "f" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"
TARGET_VERSION = "v0.3.0-rc.1"


def write_handoff(tmp_path: Path) -> HandoffDocument:
    candidate = CandidateContainerTemplate(
        container_id=OLD_CONTAINER_ID,
        name="MediaSync",
        env=("TZ=Asia/Shanghai",),
        user="",
        labels={"user.label": "keep"},
        mounts=(SafeMount("volume", "mediasync-data", "/data", False),),
        exposed_ports=("9090/tcp",),
        port_bindings={"9090/tcp": [{"HostIp": "", "HostPort": "9090"}]},
        restart_policy={"Name": "unless-stopped", "MaximumRetryCount": 0},
        network_mode="bridge",
        networks={"bridge": ("MediaSync",)},
        dns=(),
        group_add=(),
        readonly_rootfs=False,
    )
    path = UpdaterHandoffStore(
        directory=str(tmp_path / "update" / "operations")
    ).write(
        UpdaterHandoffIntent(
            schema_version=2,
            operation_id=OPERATION_ID,
            current_container_id=OLD_CONTAINER_ID,
            source_image_id=SOURCE_IMAGE_ID,
            source_image_reference="josephyjq/mediasync:v0.2.0-rc.9",
            source_version="v0.2.0-rc.9",
            source_digest=None,
            target_version=TARGET_VERSION,
            target_digest=TARGET_DIGEST,
            target_revision=TARGET_REVISION,
            target_image=f"josephyjq/mediasync@{TARGET_DIGEST}",
            candidate=candidate,
        )
    )
    from app.services.update_snapshot_service import read_handoff

    return read_handoff(path, expected_operation_id=OPERATION_ID)


class FakeEngine:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.removed: list[str] = []

    async def stop_container(self, container_id: str, *, timeout_seconds: int) -> None:
        self.events.append(f"stop:{container_id}:{timeout_seconds}")

    async def wait_container(self, container_id: str) -> int:
        self.events.append(f"wait:{container_id}")
        return 0

    async def rename_container(self, container_id: str, *, name: str) -> None:
        self.events.append(f"rename:{container_id}:{name}")

    async def create_container(self, *, name: str, config: dict[str, Any]) -> str:
        assert config["Image"] == f"josephyjq/mediasync@{TARGET_DIGEST}"
        self.events.append(f"create:{name}")
        return CANDIDATE_ID

    async def start_container(self, container_id: str) -> None:
        self.events.append(f"start:{container_id}")

    async def remove_container(self, container_id: str) -> None:
        self.events.append(f"remove:{container_id}")
        self.removed.append(container_id)

    async def inspect_container(self, _container_id: str) -> dict[str, Any]:
        raise AssertionError("orchestration test uses a fake verifier")

    async def inspect_image(self, _reference: str) -> dict[str, Any] | None:
        raise AssertionError("orchestration test uses a fake verifier")


class FakeSnapshotService:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def create(self, *, operation_id: str, handoff_path: Path) -> Path:
        assert handoff_path.name == f"{operation_id}.handoff.json"
        self.events.append("snapshot:create")
        return Path("snapshot")

    def verify(self, *, operation_id: str):
        self.events.append(f"snapshot:verify:{operation_id}")
        return Path("snapshot"), object()


class FakeJournal:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self, *, operation_id: str) -> None:
        self.events.append(f"journal:snapshotting:{operation_id}")

    def transition(self, *, operation_id: str, status: str) -> None:
        self.events.append(f"journal:{status}:{operation_id}")


class FakeVerifier:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def verify(self, _document, _preparation, candidate_id: str) -> None:
        self.events.append(f"verify:{candidate_id}")
        if self.fail:
            raise UpdaterStateMachineError("candidate failed")


class FakeCommitWaiter:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def wait(self, *, operation_id: str) -> None:
        self.events.append(f"commit:{operation_id}")


def state_machine(
    tmp_path: Path,
    *,
    verifier_fails: bool = False,
) -> tuple[UpdaterStateMachine, FakeEngine, list[str]]:
    write_handoff(tmp_path)
    events: list[str] = []
    engine = FakeEngine(events)
    return (
        UpdaterStateMachine(
            engine=engine,
            data_directory=tmp_path,
            snapshot_service=FakeSnapshotService(events),  # type: ignore[arg-type]
            candidate_service=UpdaterCandidateService(
                pending_path=tmp_path / "update" / "pending.json",
                token_factory=lambda: CANDIDATE_TOKEN,
            ),
            journal=FakeJournal(events),  # type: ignore[arg-type]
            verifier=FakeVerifier(events, fail=verifier_fails),
            commit_waiter=FakeCommitWaiter(events),
        ),
        engine,
        events,
    )


@pytest.mark.asyncio
async def test_normal_path_removes_old_container_only_after_success_commit(
    tmp_path: Path,
) -> None:
    machine, engine, events = state_machine(tmp_path)

    candidate_id = await machine.execute(operation_id=OPERATION_ID)

    assert candidate_id == CANDIDATE_ID
    assert events == [
        f"journal:snapshotting:{OPERATION_ID}",
        f"stop:{OLD_CONTAINER_ID}:90",
        f"wait:{OLD_CONTAINER_ID}",
        "snapshot:create",
        f"snapshot:verify:{OPERATION_ID}",
        f"journal:switching:{OPERATION_ID}",
        f"rename:{OLD_CONTAINER_ID}:mediasync-previous-12345678",
        "create:MediaSync",
        f"start:{CANDIDATE_ID}",
        f"journal:verifying:{OPERATION_ID}",
        f"verify:{CANDIDATE_ID}",
        f"journal:success:{OPERATION_ID}",
        f"commit:{OPERATION_ID}",
        f"remove:{OLD_CONTAINER_ID}",
    ]
    assert engine.removed == [OLD_CONTAINER_ID]


@pytest.mark.asyncio
async def test_candidate_failure_never_deletes_old_container(tmp_path: Path) -> None:
    machine, engine, events = state_machine(tmp_path, verifier_fails=True)

    with pytest.raises(UpdaterStateMachineError, match="candidate failed"):
        await machine.execute(operation_id=OPERATION_ID)

    assert engine.removed == []
    assert not any(event.startswith("journal:success") for event in events)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += seconds


class VerificationEngine:
    def __init__(self, *, restart_counts: list[int] | None = None) -> None:
        self.restart_counts = iter(restart_counts or [0])
        self.last_restart_count = 0

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        self.last_restart_count = next(self.restart_counts, self.last_restart_count)
        return {
            "Id": container_id,
            "Image": TARGET_IMAGE_ID,
            "RestartCount": self.last_restart_count,
            "Config": {"Image": f"josephyjq/mediasync@{TARGET_DIGEST}"},
            "State": {
                "Running": True,
                "StartedAt": "2026-08-03T01:00:00Z",
                "Health": {"Status": "healthy"},
            },
        }

    async def inspect_image(self, _reference: str) -> dict[str, Any]:
        return {
            "Id": TARGET_IMAGE_ID,
            "RepoDigests": [f"josephyjq/mediasync@{TARGET_DIGEST}"],
            "Config": {
                "Cmd": APPLIANCE_COMMAND,
                "Labels": {
                    "org.opencontainers.image.source": OFFICIAL_SOURCE,
                    "org.opencontainers.image.title": "MediaSync",
                    "org.opencontainers.image.version": TARGET_VERSION,
                    "org.opencontainers.image.revision": TARGET_REVISION,
                },
            },
        }


def prepare_verification(
    tmp_path: Path,
    *,
    observed_at: datetime,
):
    document = write_handoff(tmp_path)
    preparation = UpdaterCandidateService(
        pending_path=tmp_path / "update" / "pending.json",
        token_factory=lambda: CANDIDATE_TOKEN,
    ).prepare(document)
    evidence = CandidateEvidence(
        schema_version=1,
        operation_id=OPERATION_ID,
        candidate_token=CANDIDATE_TOKEN,
        mode="candidate_validation",
        version=TARGET_VERSION.removeprefix("v"),
        revision=TARGET_REVISION,
        digest=TARGET_DIGEST,
        alembic_revision="0007_update_operations",
        components={
            "launcher": True,
            "nginx": True,
            "api": True,
            "scheduler": True,
            "worker": True,
        },
        observed_at=observed_at,
    )
    write_private_json(
        tmp_path / "update" / "operations" / f"{OPERATION_ID}.candidate.json",
        evidence.model_dump(mode="json"),
    )
    return document, preparation


@pytest.mark.asyncio
async def test_candidate_verifier_requires_unchanged_stable_window(tmp_path: Path) -> None:
    document, preparation = prepare_verification(
        tmp_path,
        observed_at=datetime(2026, 8, 3, 1, 0, 1, tzinfo=UTC),
    )
    clock = FakeClock()
    verifier = CandidateHealthVerifier(
        engine=VerificationEngine(),  # type: ignore[arg-type]
        data_directory=tmp_path,
        pending_path=tmp_path / "update" / "pending.json",
        stable_seconds=2,
        timeout_seconds=5,
        poll_seconds=1,
        clock=clock,
        sleep=clock.sleep,
    )

    await verifier.verify(document, preparation, CANDIDATE_ID)

    assert clock.value == 2


@pytest.mark.asyncio
async def test_candidate_restart_during_stable_window_is_rejected(tmp_path: Path) -> None:
    document, preparation = prepare_verification(
        tmp_path,
        observed_at=datetime(2026, 8, 3, 1, 0, 1, tzinfo=UTC),
    )
    clock = FakeClock()
    verifier = CandidateHealthVerifier(
        engine=VerificationEngine(restart_counts=[0, 1]),  # type: ignore[arg-type]
        data_directory=tmp_path,
        pending_path=tmp_path / "update" / "pending.json",
        stable_seconds=2,
        timeout_seconds=5,
        poll_seconds=1,
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(UpdaterStateMachineError, match="重启或替换"):
        await verifier.verify(document, preparation, CANDIDATE_ID)


@pytest.mark.asyncio
async def test_evidence_older_than_current_candidate_start_is_rejected(
    tmp_path: Path,
) -> None:
    document, preparation = prepare_verification(
        tmp_path,
        observed_at=datetime(2026, 8, 3, 0, 59, 59, tzinfo=UTC),
    )
    clock = FakeClock()
    verifier = CandidateHealthVerifier(
        engine=VerificationEngine(),  # type: ignore[arg-type]
        data_directory=tmp_path,
        pending_path=tmp_path / "update" / "pending.json",
        stable_seconds=2,
        timeout_seconds=5,
        poll_seconds=1,
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(UpdaterStateMachineError, match="当前容器不匹配"):
        await verifier.verify(document, preparation, CANDIDATE_ID)


@pytest.mark.asyncio
async def test_commit_waiter_times_out_without_database_handshake(tmp_path: Path) -> None:
    clock = FakeClock()
    waiter = ApplianceCommitWaiter(
        data_directory=tmp_path,
        pending_path=tmp_path / "update" / "pending.json",
        timeout_seconds=2,
        poll_seconds=1,
        clock=clock,
        sleep=clock.sleep,
    )

    with pytest.raises(UpdaterStateMachineError, match="终态超时"):
        await waiter.wait(operation_id=OPERATION_ID)


@pytest.mark.asyncio
async def test_commit_waiter_requires_database_terminal_and_all_runtime_markers_removed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mediasync.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE update_operations "
        "(operation_id TEXT, status TEXT, active_slot TEXT)"
    )
    connection.execute(
        "INSERT INTO update_operations VALUES (?, 'success', NULL)",
        (OPERATION_ID,),
    )
    connection.commit()
    connection.close()
    clock = FakeClock()
    waiter = ApplianceCommitWaiter(
        data_directory=tmp_path,
        pending_path=tmp_path / "update" / "pending.json",
        timeout_seconds=2,
        poll_seconds=1,
        clock=clock,
        sleep=clock.sleep,
    )

    await waiter.wait(operation_id=OPERATION_ID)

    assert clock.value == 0
