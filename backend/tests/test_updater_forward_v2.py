from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.services.docker_capability_service import APPLIANCE_COMMAND
from app.services.update_snapshot_service import UpdaterResultJournal
from app.services.updater_candidate_service import UpdaterCandidateService
from app.services.updater_forward_v2 import UpdaterForwardV2
from app.services.updater_handoff_service import (
    CandidateContainerTemplate,
    SafeMount,
    UpdaterHandoffIntent,
    UpdaterHandoffStore,
)
from app.services.updater_rollback_v2 import UpdaterRollbackV2
from app.services.updater_state_machine import UpdaterStateMachineError

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
SOURCE_ID = "a" * 64
CANDIDATE_ID = "b" * 64
COORDINATOR_ID = "c" * 64
SOURCE_IMAGE_ID = f"sha256:{'d' * 64}"
TARGET_DIGEST = f"sha256:{'e' * 64}"
TARGET_IMAGE = f"josephyjq/mediasync@{TARGET_DIGEST}"
TARGET_REVISION = "f" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"
SOCKET_PATH = "/var/run/docker.sock"


class SimulatedCrash(BaseException):
    pass


def write_handoff(tmp_path: Path) -> None:
    UpdaterHandoffStore(
        directory=str(tmp_path / "update" / "operations")
    ).write(
        UpdaterHandoffIntent(
            schema_version=2,
            operation_id=OPERATION_ID,
            current_container_id=SOURCE_ID,
            source_image_id=SOURCE_IMAGE_ID,
            source_image_reference="josephyjq/mediasync:v0.2.0-rc.9",
            source_version="v0.2.0-rc.9",
            source_digest=None,
            target_version="v0.3.0-rc.1",
            target_digest=TARGET_DIGEST,
            target_revision=TARGET_REVISION,
            target_image=TARGET_IMAGE,
            candidate=CandidateContainerTemplate(
                container_id=SOURCE_ID,
                name="MediaSync",
                env=("TZ=Asia/Shanghai",),
                user="",
                labels={},
                mounts=(
                    SafeMount("volume", "mediasync-data", "/data", False),
                ),
                exposed_ports=("9090/tcp",),
                port_bindings={},
                restart_policy={
                    "Name": "unless-stopped",
                    "MaximumRetryCount": 0,
                },
                network_mode="bridge",
                networks={"bridge": ("MediaSync",)},
                dns=(),
                group_add=(),
                readonly_rootfs=False,
            ),
        )
    )


def volume_mount() -> dict[str, Any]:
    return {
        "Type": "volume",
        "Name": "mediasync-data",
        "Source": "/var/lib/docker/volumes/mediasync-data/_data",
        "Destination": "/data",
        "RW": True,
    }


class MutableEngine:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.containers: dict[str, dict[str, Any]] = {
            SOURCE_ID: {
                "Id": SOURCE_ID,
                "Name": "/MediaSync",
                "Image": SOURCE_IMAGE_ID,
                "Config": {"Image": "josephyjq/mediasync:v0.2.0-rc.9"},
                "HostConfig": {
                    "RestartPolicy": {
                        "Name": "unless-stopped",
                        "MaximumRetryCount": 0,
                    }
                },
                "State": {"Running": True},
                "Mounts": [volume_mount()],
            },
            COORDINATOR_ID: {
                "Id": COORDINATOR_ID,
                "Name": "/mediasync-updater-test",
                "Image": f"sha256:{'1' * 64}",
                "Config": {
                    "Image": TARGET_IMAGE,
                    "Cmd": ["python", "-m", "app.updater"],
                    "Labels": {
                        "io.mediasync.update.role": "updater",
                        "io.mediasync.update.operation": OPERATION_ID,
                    },
                },
                "HostConfig": {
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                },
                "State": {"Running": True},
                "Mounts": [
                    volume_mount(),
                    {
                        "Type": "bind",
                        "Source": SOCKET_PATH,
                        "Destination": SOCKET_PATH,
                        "RW": True,
                    },
                ],
            },
        }

    async def list_containers(self) -> list[dict[str, Any]]:
        return [
            {
                "Id": item["Id"],
                "Labels": item.get("Config", {}).get("Labels", {}),
            }
            for item in self.containers.values()
        ]

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        return self.containers[container_id]

    async def update_restart_policy(
        self,
        container_id: str,
        *,
        restart_policy: dict[str, Any],
    ) -> None:
        self.calls["restart_policy"] += 1
        self.containers[container_id]["HostConfig"]["RestartPolicy"] = restart_policy

    async def stop_container(self, container_id: str, *, timeout_seconds: int) -> None:
        assert timeout_seconds == 90
        key = "stop_candidate" if container_id == CANDIDATE_ID else "stop_old"
        self.calls[key] += 1
        self.containers[container_id]["State"]["Running"] = False

    async def wait_container(self, container_id: str) -> int:
        assert self.containers[container_id]["State"]["Running"] is False
        return 0

    async def rename_container(self, container_id: str, *, name: str) -> None:
        self.calls["rename_old"] += 1
        self.containers[container_id]["Name"] = f"/{name}"

    async def create_container(self, *, name: str, config: dict[str, Any]) -> str:
        self.calls["create_candidate"] += 1
        mounts = [
            {
                "Type": item["Type"],
                "Name": item["Source"] if item["Type"] == "volume" else None,
                "Source": item["Source"],
                "Destination": item["Target"],
                "RW": not item["ReadOnly"],
            }
            for item in config["HostConfig"]["Mounts"]
        ]
        self.containers[CANDIDATE_ID] = {
            "Id": CANDIDATE_ID,
            "Name": f"/{name}",
            "Image": f"sha256:{'2' * 64}",
            "Config": {
                "Image": config["Image"],
                "Cmd": APPLIANCE_COMMAND,
                "Env": config["Env"],
                "Labels": config["Labels"],
            },
            "HostConfig": config["HostConfig"],
            "State": {"Running": False},
            "Mounts": mounts,
        }
        return CANDIDATE_ID

    async def start_container(self, container_id: str) -> None:
        key = "start_candidate" if container_id == CANDIDATE_ID else "start_old"
        self.calls[key] += 1
        self.containers[container_id]["State"]["Running"] = True

    async def remove_container(self, container_id: str) -> None:
        key = "remove_candidate" if container_id == CANDIDATE_ID else "remove_old"
        self.calls[key] += 1
        self.containers.pop(container_id)


class FakeSnapshotService:
    def __init__(self, tmp_path: Path) -> None:
        self.backup_root = tmp_path / "backups" / "updates"
        self.calls: Counter[str] = Counter()

    def create(self, *, operation_id: str, handoff_path: Path) -> Path:
        assert handoff_path.is_file()
        self.calls["create"] += 1
        directory = self.backup_root / operation_id
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("valid", encoding="utf-8")
        return directory

    def verify(self, *, operation_id: str):
        self.calls["verify"] += 1
        directory = self.backup_root / operation_id
        if not (directory / "manifest.json").is_file():
            raise AssertionError("snapshot missing")
        return directory, object()

    def restore(self, *, operation_id: str):
        self.calls["restore"] += 1
        self.verify(operation_id=operation_id)
        return object()


class FakeVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, _document, _preparation, candidate_id: str) -> None:
        assert candidate_id == CANDIDATE_ID
        self.calls += 1


class FakeCommitWaiter:
    def __init__(self) -> None:
        self.calls = 0

    async def wait(self, *, operation_id: str) -> None:
        assert operation_id == OPERATION_ID
        self.calls += 1


class FakePreviousVerifier:
    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, _document, container_id: str) -> None:
        assert container_id == SOURCE_ID
        self.calls += 1


class OneShotFault:
    def __init__(self, event: str) -> None:
        self.event = event
        self.fired = False

    def __call__(self, event: str) -> None:
        if event == self.event and not self.fired:
            self.fired = True
            raise SimulatedCrash(event)


def executor(
    tmp_path: Path,
    *,
    engine: MutableEngine,
    snapshot: FakeSnapshotService,
    verifier: FakeVerifier,
    waiter: FakeCommitWaiter,
    fault_hook=None,
) -> UpdaterForwardV2:
    return UpdaterForwardV2(
        engine=engine,
        data_directory=tmp_path,
        socket_path=SOCKET_PATH,
        coordinator_container_id=COORDINATOR_ID,
        snapshot_service=snapshot,  # type: ignore[arg-type]
        candidate_service=UpdaterCandidateService(
            pending_path=tmp_path / "update" / "pending.json",
            token_factory=lambda: CANDIDATE_TOKEN,
        ),
        journal=UpdaterResultJournal(
            directory=str(tmp_path / "update" / "operations")
        ),
        verifier=verifier,
        commit_waiter=waiter,
        fault_hook=fault_hook,
    )


def rollback_executor(
    tmp_path: Path,
    *,
    engine: MutableEngine,
    snapshot: FakeSnapshotService,
    previous_verifier: FakePreviousVerifier,
    fault_hook=None,
) -> UpdaterRollbackV2:
    return UpdaterRollbackV2(
        engine=engine,
        data_directory=tmp_path,
        socket_path=SOCKET_PATH,
        coordinator_container_id=COORDINATOR_ID,
        snapshot_service=snapshot,  # type: ignore[arg-type]
        candidate_service=UpdaterCandidateService(
            pending_path=tmp_path / "update" / "pending.json",
            token_factory=lambda: CANDIDATE_TOKEN,
        ),
        journal=UpdaterResultJournal(
            directory=str(tmp_path / "update" / "operations")
        ),
        previous_verifier=previous_verifier,
        fault_hook=fault_hook,
    )


@pytest.mark.asyncio
async def test_normal_path_persists_all_v2_checkpoints_and_cleans_old(
    tmp_path: Path,
) -> None:
    write_handoff(tmp_path)
    engine = MutableEngine()
    snapshot = FakeSnapshotService(tmp_path)
    verifier = FakeVerifier()
    waiter = FakeCommitWaiter()

    candidate_id = await executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        verifier=verifier,
        waiter=waiter,
    ).execute(operation_id=OPERATION_ID)

    result = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert candidate_id == CANDIDATE_ID
    assert result.schema_version == 2
    assert result.status == "success"
    assert result.checkpoint == "commit_requested"
    assert result.sequence == 12
    assert result.candidate_container_id == CANDIDATE_ID
    assert SOURCE_ID not in engine.containers
    assert engine.calls == Counter({
        "restart_policy": 1,
        "stop_old": 1,
        "rename_old": 1,
        "create_candidate": 1,
        "start_candidate": 1,
        "remove_old": 1,
    })


RECOVERABLE_FAULTS = (
    "before:initialized",
    "after_checkpoint:initialized",
    "before:old_restart_fenced",
    "after_effect:old_restart_fenced",
    "before_checkpoint:snapshotting:old_restart_fenced",
    "after_checkpoint:snapshotting:old_restart_fenced",
    "before:old_stopped",
    "after_effect:old_stopped",
    "before_checkpoint:snapshotting:old_stopped",
    "after_checkpoint:snapshotting:old_stopped",
    "before:pending_ready",
    "after_effect:pending_ready",
    "before_checkpoint:snapshotting:pending_ready",
    "after_checkpoint:snapshotting:pending_ready",
    "before:snapshot_verified",
    "after_effect:snapshot_verified",
    "before_checkpoint:snapshotting:snapshot_verified",
    "after_checkpoint:snapshotting:snapshot_verified",
    "before:old_renamed",
    "after_effect:old_renamed",
    "before_checkpoint:switching:old_renamed",
    "after_checkpoint:switching:old_renamed",
    "before:candidate_created",
    "after_effect:candidate_created",
    "before_checkpoint:switching:candidate_created",
    "after_checkpoint:switching:candidate_created",
    "before:candidate_started",
    "after_effect:candidate_started",
    "before_checkpoint:switching:candidate_started",
    "after_checkpoint:switching:candidate_started",
    "before_checkpoint:verifying:candidate_started",
    "after_checkpoint:verifying:candidate_started",
    "before:candidate_verified",
    "after_effect:candidate_verified",
    "before_checkpoint:verifying:candidate_verified",
    "after_checkpoint:verifying:candidate_verified",
    "before_checkpoint:commit_requested:commit_requested",
    "after_checkpoint:commit_requested:commit_requested",
    "before:commit_confirmed",
    "after_effect:commit_confirmed",
    "before_checkpoint:success:commit_requested",
    "after_checkpoint:success:commit_requested",
    "before:old_removed",
    "after_effect:old_removed",
)


@pytest.mark.asyncio
async def test_forward_fault_matrix_covers_every_emitted_hook(tmp_path: Path) -> None:
    write_handoff(tmp_path)
    events: list[str] = []

    await executor(
        tmp_path,
        engine=MutableEngine(),
        snapshot=FakeSnapshotService(tmp_path),
        verifier=FakeVerifier(),
        waiter=FakeCommitWaiter(),
        fault_hook=events.append,
    ).execute(operation_id=OPERATION_ID)

    assert set(events) <= set(RECOVERABLE_FAULTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_event", RECOVERABLE_FAULTS)
async def test_interrupted_forward_step_resumes_without_duplicate_side_effects(
    tmp_path: Path,
    fault_event: str,
) -> None:
    write_handoff(tmp_path)
    engine = MutableEngine()
    snapshot = FakeSnapshotService(tmp_path)
    verifier = FakeVerifier()
    waiter = FakeCommitWaiter()
    fault = OneShotFault(fault_event)

    with pytest.raises(SimulatedCrash):
        await executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            verifier=verifier,
            waiter=waiter,
            fault_hook=fault,
        ).execute(operation_id=OPERATION_ID)

    candidate_id = await executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        verifier=verifier,
        waiter=waiter,
    ).execute(operation_id=OPERATION_ID)

    assert candidate_id == CANDIDATE_ID
    assert engine.calls["restart_policy"] == 1
    assert engine.calls["stop_old"] == 1
    assert engine.calls["rename_old"] == 1
    assert engine.calls["create_candidate"] == 1
    assert engine.calls["start_candidate"] == 1
    assert engine.calls["remove_old"] == 1
    assert snapshot.calls["create"] == 1


@pytest.mark.asyncio
async def test_resume_refuses_tampered_candidate_before_start(tmp_path: Path) -> None:
    write_handoff(tmp_path)
    engine = MutableEngine()
    snapshot = FakeSnapshotService(tmp_path)
    verifier = FakeVerifier()
    waiter = FakeCommitWaiter()
    fault = OneShotFault("after_checkpoint:switching:candidate_created")

    with pytest.raises(SimulatedCrash):
        await executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            verifier=verifier,
            waiter=waiter,
            fault_hook=fault,
        ).execute(operation_id=OPERATION_ID)
    engine.containers[CANDIDATE_ID]["Config"]["Env"] = [
        "MEDIASYNC_CANDIDATE_TOKEN=tampered-token-0123456789-abcdef"
    ]

    with pytest.raises(UpdaterStateMachineError, match="身份与 v2 检查点冲突"):
        await executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            verifier=verifier,
            waiter=waiter,
        ).execute(operation_id=OPERATION_ID)

    assert engine.calls["start_candidate"] == 0


async def prepare_candidate_for_rollback(
    tmp_path: Path,
) -> tuple[MutableEngine, FakeSnapshotService, FakePreviousVerifier]:
    write_handoff(tmp_path)
    engine = MutableEngine()
    snapshot = FakeSnapshotService(tmp_path)
    verifier = FakeVerifier()
    waiter = FakeCommitWaiter()
    fault = OneShotFault("after_checkpoint:switching:candidate_started")
    with pytest.raises(SimulatedCrash):
        await executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            verifier=verifier,
            waiter=waiter,
            fault_hook=fault,
        ).execute(operation_id=OPERATION_ID)
    return engine, snapshot, FakePreviousVerifier()


@pytest.mark.asyncio
async def test_rollback_v2_restores_old_container_and_publishes_terminal(
    tmp_path: Path,
) -> None:
    engine, snapshot, previous = await prepare_candidate_for_rollback(tmp_path)
    evidence = tmp_path / "update" / "operations" / f"{OPERATION_ID}.candidate.json"
    evidence.write_text("stale", encoding="utf-8")

    await rollback_executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        previous_verifier=previous,
    ).execute(operation_id=OPERATION_ID)

    result = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    source = engine.containers[SOURCE_ID]
    assert result.status == "rolled_back"
    assert result.checkpoint == "rollback_published"
    assert result.error_code == "update_forward_incomplete"
    assert result.public_error_message == (
        "更新在“验证新版本容器”阶段未能完成，已自动恢复到更新前版本"
    )
    assert result.sequence == 19
    assert CANDIDATE_ID not in engine.containers
    assert source["Name"] == "/MediaSync"
    assert source["State"]["Running"] is True
    assert source["HostConfig"]["RestartPolicy"] == {
        "Name": "unless-stopped",
        "MaximumRetryCount": 0,
    }
    assert engine.calls["stop_candidate"] == 1
    assert engine.calls["remove_candidate"] == 1
    assert engine.calls["start_old"] == 1
    assert snapshot.calls["restore"] == 1
    assert previous.calls == 1
    assert not evidence.exists()


ROLLBACK_FAULTS = (
    "before:rollback_started",
    "after_effect:rollback_started",
    "before_checkpoint:rolling_back:rollback_started",
    "after_checkpoint:rolling_back:rollback_started",
    "before:candidate_stopped",
    "after_effect:candidate_stopped",
    "before_checkpoint:rolling_back:candidate_stopped",
    "after_checkpoint:rolling_back:candidate_stopped",
    "before:candidate_removed",
    "after_effect:candidate_removed",
    "before_checkpoint:rolling_back:candidate_removed",
    "after_checkpoint:rolling_back:candidate_removed",
    "before:snapshot_restored",
    "after_effect:snapshot_restored",
    "before_checkpoint:rolling_back:snapshot_restored",
    "after_checkpoint:rolling_back:snapshot_restored",
    "before:candidate_evidence_removed",
    "after_effect:candidate_evidence_removed",
    "before_checkpoint:rolling_back:candidate_evidence_removed",
    "after_checkpoint:rolling_back:candidate_evidence_removed",
    "before:old_name_restored",
    "after_effect:old_name_restored",
    "before_checkpoint:rolling_back:old_name_restored",
    "after_checkpoint:rolling_back:old_name_restored",
    "before:old_policy_restored",
    "after_effect:old_policy_restored",
    "before_checkpoint:rolling_back:old_policy_restored",
    "after_checkpoint:rolling_back:old_policy_restored",
    "before:old_started",
    "after_effect:old_started",
    "before_checkpoint:rolling_back:old_started",
    "after_checkpoint:rolling_back:old_started",
    "before:old_verified",
    "after_effect:old_verified",
    "before_checkpoint:rolling_back:old_verified",
    "after_checkpoint:rolling_back:old_verified",
    "before:rollback_published",
    "after_effect:rollback_published",
    "before_checkpoint:rolling_back:rollback_published",
    "after_checkpoint:rolling_back:rollback_published",
    "before_checkpoint:rolled_back:rollback_published",
    "after_checkpoint:rolled_back:rollback_published",
)


@pytest.mark.asyncio
async def test_rollback_fault_matrix_covers_every_emitted_hook(tmp_path: Path) -> None:
    engine, snapshot, previous = await prepare_candidate_for_rollback(tmp_path)
    events: list[str] = []

    await rollback_executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        previous_verifier=previous,
        fault_hook=events.append,
    ).execute(operation_id=OPERATION_ID)

    assert set(events) <= set(ROLLBACK_FAULTS)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_event", ROLLBACK_FAULTS)
async def test_interrupted_rollback_resumes_same_attempt(
    tmp_path: Path,
    fault_event: str,
) -> None:
    engine, snapshot, previous = await prepare_candidate_for_rollback(tmp_path)
    fault = OneShotFault(fault_event)

    with pytest.raises(SimulatedCrash):
        await rollback_executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            previous_verifier=previous,
            fault_hook=fault,
        ).execute(operation_id=OPERATION_ID)

    await rollback_executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        previous_verifier=previous,
    ).execute(operation_id=OPERATION_ID)

    result = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert result.status == "rolled_back"
    assert engine.calls["stop_candidate"] == 1
    assert engine.calls["remove_candidate"] == 1
    assert engine.calls["start_old"] == 1
    assert engine.calls["rename_old"] == 2
    assert engine.calls["restart_policy"] == 2
    assert snapshot.calls["restore"] in {1, 2}


@pytest.mark.asyncio
async def test_rollback_without_candidate_snapshot_or_rename_is_valid_noop(
    tmp_path: Path,
) -> None:
    write_handoff(tmp_path)
    engine = MutableEngine()
    snapshot = FakeSnapshotService(tmp_path)
    forward_fault = OneShotFault("after_checkpoint:snapshotting:old_stopped")
    with pytest.raises(SimulatedCrash):
        await executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            verifier=FakeVerifier(),
            waiter=FakeCommitWaiter(),
            fault_hook=forward_fault,
        ).execute(operation_id=OPERATION_ID)
    previous = FakePreviousVerifier()

    await rollback_executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        previous_verifier=previous,
    ).execute(operation_id=OPERATION_ID)

    result = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert result.status == "rolled_back"
    assert result.candidate_token_hash is not None
    assert engine.calls["create_candidate"] == 0
    assert engine.calls["remove_candidate"] == 0
    assert engine.calls["rename_old"] == 0
    assert snapshot.calls["restore"] == 0
    assert (tmp_path / "update" / "pending.json").is_file()


@pytest.mark.asyncio
async def test_rollback_discovers_candidate_created_before_id_checkpoint(
    tmp_path: Path,
) -> None:
    write_handoff(tmp_path)
    engine = MutableEngine()
    snapshot = FakeSnapshotService(tmp_path)
    forward_fault = OneShotFault("after_effect:candidate_created")
    with pytest.raises(SimulatedCrash):
        await executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            verifier=FakeVerifier(),
            waiter=FakeCommitWaiter(),
            fault_hook=forward_fault,
        ).execute(operation_id=OPERATION_ID)
    before = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert before.candidate_container_id is None
    assert CANDIDATE_ID in engine.containers

    await rollback_executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        previous_verifier=FakePreviousVerifier(),
    ).execute(operation_id=OPERATION_ID)

    after = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert after.status == "rolled_back"
    assert after.candidate_container_id == CANDIDATE_ID
    assert CANDIDATE_ID not in engine.containers


@pytest.mark.asyncio
async def test_candidate_identity_conflict_enters_rollback_failed(
    tmp_path: Path,
) -> None:
    engine, snapshot, previous = await prepare_candidate_for_rollback(tmp_path)
    engine.containers[CANDIDATE_ID]["Config"]["Env"] = [
        "MEDIASYNC_CANDIDATE_TOKEN=tampered-token-0123456789-abcdef"
    ]

    with pytest.raises(UpdaterStateMachineError, match="身份无法安全确认"):
        await rollback_executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            previous_verifier=previous,
        ).execute(operation_id=OPERATION_ID)

    result = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert result.status == "rollback_failed"
    assert CANDIDATE_ID in engine.containers
    assert engine.calls["stop_candidate"] == 0


@pytest.mark.asyncio
async def test_transient_engine_error_keeps_rollback_retriable(
    tmp_path: Path,
) -> None:
    engine, snapshot, previous = await prepare_candidate_for_rollback(tmp_path)
    original_remove = engine.remove_container
    failed_once = False

    async def transient_remove(container_id: str) -> None:
        nonlocal failed_once
        if container_id == CANDIDATE_ID and not failed_once:
            failed_once = True
            raise RuntimeError("docker temporarily unavailable")
        await original_remove(container_id)

    engine.remove_container = transient_remove  # type: ignore[method-assign]

    with pytest.raises(UpdaterStateMachineError, match="可从当前检查点重试"):
        await rollback_executor(
            tmp_path,
            engine=engine,
            snapshot=snapshot,
            previous_verifier=previous,
        ).execute(operation_id=OPERATION_ID)

    current = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert current.status == "rolling_back"
    assert current.checkpoint == "candidate_stopped"

    await rollback_executor(
        tmp_path,
        engine=engine,
        snapshot=snapshot,
        previous_verifier=previous,
    ).execute(operation_id=OPERATION_ID)
    terminal = UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    ).read(operation_id=OPERATION_ID)
    assert terminal.status == "rolled_back"
