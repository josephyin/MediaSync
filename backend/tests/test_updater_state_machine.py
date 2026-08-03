from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
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
    PreviousContainerHealthVerifier,
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
    def __init__(
        self,
        events: list[str],
        *,
        restart_policy_fails: bool = False,
        fail_at: str | None = None,
    ) -> None:
        self.events = events
        self.removed: list[str] = []
        self.restart_policy_fails = restart_policy_fails
        self.fail_at = fail_at

    def maybe_fail(self, step: str) -> None:
        if self.fail_at == step:
            from app.services.docker_capability_service import DockerEngineError

            raise DockerEngineError(f"{step} failed")

    async def update_restart_policy(
        self,
        container_id: str,
        *,
        restart_policy: dict[str, Any],
    ) -> None:
        self.events.append(f"restart-policy:{container_id}:{restart_policy['Name']}")
        if self.restart_policy_fails:
            from app.services.docker_capability_service import DockerEngineError

            raise DockerEngineError("policy failed")

    async def stop_container(self, container_id: str, *, timeout_seconds: int) -> None:
        self.events.append(f"stop:{container_id}:{timeout_seconds}")
        self.maybe_fail("stop_candidate" if container_id == CANDIDATE_ID else "stop_old")

    async def wait_container(self, container_id: str) -> int:
        self.events.append(f"wait:{container_id}")
        return 0

    async def rename_container(self, container_id: str, *, name: str) -> None:
        self.events.append(f"rename:{container_id}:{name}")
        self.maybe_fail("rename_restore" if name == "MediaSync" else "rename_previous")

    async def create_container(self, *, name: str, config: dict[str, Any]) -> str:
        assert config["Image"] == f"josephyjq/mediasync@{TARGET_DIGEST}"
        self.events.append(f"create:{name}")
        self.maybe_fail("create_candidate")
        return CANDIDATE_ID

    async def start_container(self, container_id: str) -> None:
        self.events.append(f"start:{container_id}")
        self.maybe_fail(
            "start_candidate" if container_id == CANDIDATE_ID else "start_previous"
        )

    async def remove_container(self, container_id: str) -> None:
        self.events.append(f"remove:{container_id}")
        self.removed.append(container_id)

    async def inspect_container(self, _container_id: str) -> dict[str, Any]:
        raise AssertionError("orchestration test uses a fake verifier")

    async def inspect_image(self, _reference: str) -> dict[str, Any] | None:
        raise AssertionError("orchestration test uses a fake verifier")


class FakeSnapshotService:
    def __init__(self, events: list[str], *, create_fails: bool = False) -> None:
        self.events = events
        self.create_fails = create_fails

    def create(self, *, operation_id: str, handoff_path: Path) -> Path:
        assert handoff_path.name == f"{operation_id}.handoff.json"
        self.events.append("snapshot:create")
        if self.create_fails:
            from app.services.update_snapshot_service import UpdateSnapshotError

            raise UpdateSnapshotError("snapshot failed")
        return Path("snapshot")

    def verify(self, *, operation_id: str):
        self.events.append(f"snapshot:verify:{operation_id}")
        return Path("snapshot"), object()

    def restore(self, *, operation_id: str):
        self.events.append(f"snapshot:restore:{operation_id}")
        return object()


class FakeJournal:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.status: str | None = None

    def start(self, *, operation_id: str) -> None:
        self.status = "snapshotting"
        self.events.append(f"journal:snapshotting:{operation_id}")

    def read(self, *, operation_id: str):
        assert self.status is not None
        return SimpleNamespace(operation_id=operation_id, status=self.status)

    def transition(self, *, operation_id: str, status: str, **_kwargs) -> None:
        self.status = status
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
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def wait(self, *, operation_id: str) -> None:
        self.events.append(f"commit:{operation_id}")
        if self.fail:
            raise UpdaterStateMachineError("等待 Appliance 提交更新终态超时")


class FakePreviousVerifier:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail

    async def verify(self, _document, container_id: str) -> None:
        self.events.append(f"verify-previous:{container_id}")
        if self.fail:
            raise UpdaterStateMachineError("previous failed")


def state_machine(
    tmp_path: Path,
    *,
    verifier_fails: bool = False,
    commit_fails: bool = False,
    restart_policy_fails: bool = False,
    snapshot_fails: bool = False,
    previous_verifier_fails: bool = False,
    engine_fails_at: str | None = None,
) -> tuple[UpdaterStateMachine, FakeEngine, list[str]]:
    write_handoff(tmp_path)
    events: list[str] = []
    engine = FakeEngine(
        events,
        restart_policy_fails=restart_policy_fails,
        fail_at=engine_fails_at,
    )
    return (
        UpdaterStateMachine(
            engine=engine,
            data_directory=tmp_path,
            snapshot_service=FakeSnapshotService(  # type: ignore[arg-type]
                events,
                create_fails=snapshot_fails,
            ),
            candidate_service=UpdaterCandidateService(
                pending_path=tmp_path / "update" / "pending.json",
                token_factory=lambda: CANDIDATE_TOKEN,
            ),
            journal=FakeJournal(events),  # type: ignore[arg-type]
            verifier=FakeVerifier(events, fail=verifier_fails),
            previous_verifier=FakePreviousVerifier(
                events,
                fail=previous_verifier_fails,
            ),
            commit_waiter=FakeCommitWaiter(events, fail=commit_fails),
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
        f"restart-policy:{OLD_CONTAINER_ID}:no",
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
        f"journal:commit_requested:{OPERATION_ID}",
        f"commit:{OPERATION_ID}",
        f"journal:success:{OPERATION_ID}",
        f"remove:{OLD_CONTAINER_ID}",
    ]
    assert engine.removed == [OLD_CONTAINER_ID]


@pytest.mark.asyncio
async def test_candidate_failure_rolls_back_without_deleting_old_container(
    tmp_path: Path,
) -> None:
    machine, engine, events = state_machine(tmp_path, verifier_fails=True)
    evidence = (
        tmp_path / "update" / "operations" / f"{OPERATION_ID}.candidate.json"
    )
    evidence.write_text("stale", encoding="utf-8")

    with pytest.raises(UpdaterStateMachineError, match="已自动回滚"):
        await machine.execute(operation_id=OPERATION_ID)

    assert engine.removed == [CANDIDATE_ID]
    assert OLD_CONTAINER_ID not in engine.removed
    assert f"snapshot:restore:{OPERATION_ID}" in events
    assert f"verify-previous:{OLD_CONTAINER_ID}" in events
    assert f"journal:rolled_back:{OPERATION_ID}" in events
    assert not any(event.startswith("journal:success") for event in events)
    assert not evidence.exists()
    assert (tmp_path / "update" / "pending.json").exists()
    assert (
        tmp_path / "update" / "operations" / f"{OPERATION_ID}.handoff.json"
    ).exists()
    ordered = [
        f"stop:{CANDIDATE_ID}:90",
        f"remove:{CANDIDATE_ID}",
        f"snapshot:restore:{OPERATION_ID}",
        f"rename:{OLD_CONTAINER_ID}:MediaSync",
        f"restart-policy:{OLD_CONTAINER_ID}:unless-stopped",
        f"start:{OLD_CONTAINER_ID}",
        f"verify-previous:{OLD_CONTAINER_ID}",
        f"journal:rolled_back:{OPERATION_ID}",
    ]
    assert [events.index(item) for item in ordered] == sorted(
        events.index(item) for item in ordered
    )


@pytest.mark.asyncio
async def test_snapshot_failure_restarts_old_without_restoring_incomplete_snapshot(
    tmp_path: Path,
) -> None:
    machine, engine, events = state_machine(tmp_path, snapshot_fails=True)

    with pytest.raises(UpdaterStateMachineError, match="已自动回滚"):
        await machine.execute(operation_id=OPERATION_ID)

    assert not any(event.startswith("snapshot:restore") for event in events)
    assert f"restart-policy:{OLD_CONTAINER_ID}:unless-stopped" in events
    assert f"start:{OLD_CONTAINER_ID}" in events
    assert f"journal:rolled_back:{OPERATION_ID}" in events
    assert engine.removed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine_fails_at",
    ["rename_previous", "create_candidate", "start_candidate"],
)
async def test_switch_failures_restore_previous_container(
    tmp_path: Path,
    engine_fails_at: str,
) -> None:
    machine, engine, events = state_machine(
        tmp_path,
        engine_fails_at=engine_fails_at,
    )

    with pytest.raises(UpdaterStateMachineError, match="已自动回滚"):
        await machine.execute(operation_id=OPERATION_ID)

    assert f"snapshot:restore:{OPERATION_ID}" in events
    assert f"start:{OLD_CONTAINER_ID}" in events
    assert f"verify-previous:{OLD_CONTAINER_ID}" in events
    assert f"journal:rolled_back:{OPERATION_ID}" in events
    assert OLD_CONTAINER_ID not in engine.removed


@pytest.mark.asyncio
async def test_candidate_stop_failure_does_not_restore_snapshot_or_start_old(
    tmp_path: Path,
) -> None:
    machine, _engine, events = state_machine(
        tmp_path,
        verifier_fails=True,
        engine_fails_at="stop_candidate",
    )

    with pytest.raises(UpdaterStateMachineError, match="自动回滚失败"):
        await machine.execute(operation_id=OPERATION_ID)

    assert not any(event.startswith("snapshot:restore") for event in events)
    assert f"start:{OLD_CONTAINER_ID}" not in events
    assert f"journal:rollback_failed:{OPERATION_ID}" in events


@pytest.mark.asyncio
async def test_failed_previous_health_enters_manual_recovery_terminal(
    tmp_path: Path,
) -> None:
    machine, engine, events = state_machine(
        tmp_path,
        verifier_fails=True,
        previous_verifier_fails=True,
    )

    with pytest.raises(UpdaterStateMachineError, match="自动回滚失败"):
        await machine.execute(operation_id=OPERATION_ID)

    assert f"journal:rollback_failed:{OPERATION_ID}" in events
    assert f"journal:rolled_back:{OPERATION_ID}" not in events
    assert OLD_CONTAINER_ID not in engine.removed
    assert (tmp_path / "update" / "pending.json").exists()
    assert (
        tmp_path / "update" / "operations" / f"{OPERATION_ID}.handoff.json"
    ).exists()


@pytest.mark.asyncio
async def test_commit_timeout_keeps_candidate_and_previous_container(
    tmp_path: Path,
) -> None:
    machine, engine, events = state_machine(tmp_path, commit_fails=True)

    with pytest.raises(UpdaterStateMachineError, match="等待提交确认"):
        await machine.execute(operation_id=OPERATION_ID)

    assert f"journal:commit_requested:{OPERATION_ID}" in events
    assert not any(event.startswith("journal:success") for event in events)
    assert engine.removed == []


@pytest.mark.asyncio
async def test_restart_policy_fencing_failure_does_not_stop_old_container(
    tmp_path: Path,
) -> None:
    machine, engine, events = state_machine(tmp_path, restart_policy_fails=True)

    with pytest.raises(UpdaterStateMachineError, match="隔离旧容器重启策略"):
        await machine.execute(operation_id=OPERATION_ID)

    assert events == [
        f"journal:snapshotting:{OPERATION_ID}",
        f"restart-policy:{OLD_CONTAINER_ID}:no",
    ]
    assert engine.removed == []


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


class PreviousVerificationEngine:
    def __init__(
        self,
        *,
        restart_counts: list[int] | None = None,
        components: dict[str, bool] | None = None,
    ) -> None:
        self.restart_counts = iter(restart_counts or [0])
        self.last_restart_count = 0
        self.components = components or {
            "launcher": True,
            "nginx": True,
            "api": True,
            "scheduler": True,
            "worker": True,
        }

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        import json

        self.last_restart_count = next(self.restart_counts, self.last_restart_count)
        return {
            "Id": container_id,
            "Name": "/MediaSync",
            "Image": SOURCE_IMAGE_ID,
            "RestartCount": self.last_restart_count,
            "State": {
                "Running": True,
                "StartedAt": "2026-08-03T01:00:00Z",
                "Health": {
                    "Status": "healthy",
                    "Log": [{"Output": json.dumps(self.components)}],
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
async def test_previous_verifier_requires_stable_identity_and_five_components(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    clock = FakeClock()
    verifier = PreviousContainerHealthVerifier(
        engine=PreviousVerificationEngine(),  # type: ignore[arg-type]
        stable_seconds=2,
        timeout_seconds=5,
        poll_seconds=1,
        clock=clock,
        sleep=clock.sleep,
    )

    await verifier.verify(document, OLD_CONTAINER_ID)

    assert clock.value == 2


@pytest.mark.asyncio
async def test_previous_verifier_rejects_incomplete_component_health(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    verifier = PreviousContainerHealthVerifier(
        engine=PreviousVerificationEngine(  # type: ignore[arg-type]
            components={
                "launcher": True,
                "nginx": True,
                "api": True,
                "scheduler": True,
                "worker": False,
            }
        ),
        stable_seconds=1,
        timeout_seconds=2,
        poll_seconds=1,
    )

    with pytest.raises(UpdaterStateMachineError, match="五组件未全部健康"):
        await verifier.verify(document, OLD_CONTAINER_ID)


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
