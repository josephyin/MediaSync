from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.services.docker_capability_service import APPLIANCE_COMMAND
from app.services.update_snapshot_service import (
    UpdaterResultJournal,
    read_handoff,
)
from app.services.updater_candidate_service import UpdaterCandidateService
from app.services.updater_coordinator_service import (
    ExitedUpdaterCleanupObserver,
    ExitedUpdaterCleanupService,
    UpdaterCoordinator,
)
from app.services.updater_handoff_service import (
    CandidateContainerTemplate,
    SafeMount,
    UpdaterHandoffIntent,
    UpdaterHandoffStore,
)
from app.services.updater_process_lock import UpdaterProcessLockError

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
SOURCE_ID = "a" * 64
CANDIDATE_ID = "b" * 64
COORDINATOR_ID = "c" * 64
OLD_COORDINATOR_ID = "d" * 64
SOURCE_IMAGE_ID = f"sha256:{'e' * 64}"
TARGET_DIGEST = f"sha256:{'f' * 64}"
TARGET_IMAGE = f"josephyjq/mediasync@{TARGET_DIGEST}"
TARGET_REVISION = "1" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"
SOCKET_PATH = "/var/run/docker.sock"


def write_handoff(tmp_path: Path):
    path = UpdaterHandoffStore(
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
                env=(),
                user="",
                labels={},
                mounts=(
                    SafeMount("volume", "mediasync-data", "/data", False),
                ),
                exposed_ports=(),
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
    return read_handoff(path, expected_operation_id=OPERATION_ID)


def data_mount() -> dict[str, Any]:
    return {
        "Type": "volume",
        "Name": "mediasync-data",
        "Source": "/var/lib/docker/volumes/mediasync-data/_data",
        "Destination": "/data",
        "RW": True,
    }


def helper_container(
    container_id: str = COORDINATOR_ID,
    *,
    running: bool = True,
    policy: str = "unless-stopped",
) -> dict[str, Any]:
    return {
        "Id": container_id,
        "Name": f"/mediasync-updater-{container_id[:8]}",
        "Image": f"sha256:{'2' * 64}",
        "Config": {
            "Image": TARGET_IMAGE,
            "Cmd": ["python", "-m", "app.updater"],
            "Env": [f"MEDIASYNC_UPDATE_OPERATION_ID={OPERATION_ID}"],
            "Labels": {
                "io.mediasync.update.role": "updater",
                "io.mediasync.update.operation": OPERATION_ID,
            },
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": policy, "MaximumRetryCount": 0},
        },
        "State": {"Running": running},
        "Mounts": [
            data_mount(),
            {
                "Type": "bind",
                "Source": SOCKET_PATH,
                "Destination": SOCKET_PATH,
                "RW": True,
            },
        ],
    }


def source_container(*, running: bool = True, renamed: bool = False) -> dict[str, Any]:
    return {
        "Id": SOURCE_ID,
        "Name": "/mediasync-previous-12345678" if renamed else "/MediaSync",
        "Image": SOURCE_IMAGE_ID,
        "Config": {"Image": "josephyjq/mediasync:v0.2.0-rc.9"},
        "HostConfig": {
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}
        },
        "State": {"Running": running},
        "Mounts": [data_mount()],
    }


def candidate_container(*, running: bool = False) -> dict[str, Any]:
    return {
        "Id": CANDIDATE_ID,
        "Name": "/MediaSync",
        "Image": f"sha256:{'3' * 64}",
        "Config": {
            "Image": TARGET_IMAGE,
            "Cmd": APPLIANCE_COMMAND,
            "Env": [f"MEDIASYNC_CANDIDATE_TOKEN={CANDIDATE_TOKEN}"],
            "Labels": {
                "io.mediasync.update.role": "candidate",
                "io.mediasync.update.operation": OPERATION_ID,
            },
        },
        "HostConfig": {},
        "State": {"Running": running},
        "Mounts": [data_mount()],
    }


class FakeEngine:
    def __init__(self, containers: list[dict[str, Any]]) -> None:
        self.containers = {item["Id"]: item for item in containers}
        self.updated: list[str] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        self.fail_disarm = False
        self.start_effective = True

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
        if self.fail_disarm and container_id == COORDINATOR_ID:
            raise RuntimeError("update failed")
        self.updated.append(container_id)
        self.containers[container_id]["HostConfig"]["RestartPolicy"] = restart_policy

    async def start_container(self, container_id: str) -> None:
        self.started.append(container_id)
        if self.start_effective:
            self.containers[container_id]["State"]["Running"] = True

    async def remove_container(self, container_id: str) -> None:
        self.removed.append(container_id)
        self.containers.pop(container_id)


class FakeLock:
    def __init__(self, *, busy: bool = False) -> None:
        self.busy = busy
        self.released = False

    def acquire(self) -> None:
        if self.busy:
            raise UpdaterProcessLockError("busy")

    def release(self) -> None:
        self.released = True


class FakeExecutor:
    def __init__(self, calls: list[tuple[str, str]], kind: str) -> None:
        self.calls = calls
        self.kind = kind

    async def execute(self, *, operation_id: str) -> None:
        self.calls.append((self.kind, operation_id))


def coordinator(
    tmp_path: Path,
    *,
    engine: FakeEngine,
    journal: UpdaterResultJournal,
    lock: FakeLock | None = None,
) -> tuple[UpdaterCoordinator, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []
    return (
        UpdaterCoordinator(
            engine=engine,
            data_directory=tmp_path,
            pending_path=tmp_path / "update" / "pending.json",
            socket_path=SOCKET_PATH,
            hostname=COORDINATOR_ID[:12],
            operation_id=OPERATION_ID,
            journal=journal,
            candidate_service=UpdaterCandidateService(
                pending_path=tmp_path / "update" / "pending.json",
                token_factory=lambda: CANDIDATE_TOKEN,
            ),
            forward_factory=lambda _id: FakeExecutor(calls, "forward"),
            rollback_factory=lambda _id: FakeExecutor(calls, "rollback"),
            lock_factory=(lambda: lock) if lock is not None else None,  # type: ignore[arg-type]
        ),
        calls,
    )


def journal(tmp_path: Path) -> UpdaterResultJournal:
    return UpdaterResultJournal(
        directory=str(tmp_path / "update" / "operations")
    )


def advance_to_commit_requested(tmp_path: Path, engine: FakeEngine) -> None:
    document = write_handoff(tmp_path)
    store = journal(tmp_path)
    store.start_v2(document=document, coordinator_container_id=COORDINATOR_ID)
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="snapshotting",
        checkpoint="old_restart_fenced",
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="snapshotting",
        checkpoint="old_stopped",
    )
    preparation = UpdaterCandidateService(
        pending_path=tmp_path / "update" / "pending.json",
        token_factory=lambda: CANDIDATE_TOKEN,
    ).prepare(document)
    token_hash = "sha256:" + hashlib.sha256(
        preparation.marker.candidate_token.encode()
    ).hexdigest()
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="snapshotting",
        checkpoint="pending_ready",
        candidate_token_hash=token_hash,
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="snapshotting",
        checkpoint="snapshot_verified",
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="switching",
        checkpoint="old_renamed",
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="switching",
        checkpoint="candidate_created",
        candidate_container_id=CANDIDATE_ID,
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="switching",
        checkpoint="candidate_started",
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="verifying",
        checkpoint="candidate_started",
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="verifying",
        checkpoint="candidate_verified",
    )
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="commit_requested",
        checkpoint="commit_requested",
    )
    engine.containers[SOURCE_ID] = source_container(running=False, renamed=True)
    engine.containers[CANDIDATE_ID] = candidate_container(running=False)


@pytest.mark.asyncio
async def test_lock_loser_has_no_docker_side_effect(tmp_path: Path) -> None:
    write_handoff(tmp_path)
    engine = FakeEngine([helper_container(), source_container()])
    lock = FakeLock(busy=True)
    service, calls = coordinator(
        tmp_path,
        engine=engine,
        journal=journal(tmp_path),
        lock=lock,
    )

    outcome = await service.run_once()

    assert outcome == "waiting_for_lock"
    assert calls == []
    assert engine.updated == []


@pytest.mark.asyncio
async def test_first_execution_routes_forward_and_disarms_helper(tmp_path: Path) -> None:
    write_handoff(tmp_path)
    engine = FakeEngine([helper_container(), source_container()])
    lock = FakeLock()
    service, calls = coordinator(
        tmp_path,
        engine=engine,
        journal=journal(tmp_path),
        lock=lock,
    )

    outcome = await service.run_once()

    assert outcome == "completed"
    assert calls == [("forward", OPERATION_ID)]
    assert engine.updated == [COORDINATOR_ID]
    assert lock.released is True


@pytest.mark.asyncio
async def test_same_helper_restart_does_not_increment_recovery_generation(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    store = journal(tmp_path)
    store.start_v2(document=document, coordinator_container_id=COORDINATOR_ID)
    engine = FakeEngine([helper_container(), source_container()])
    service, calls = coordinator(tmp_path, engine=engine, journal=store)

    outcome = await service.run_once()

    current = store.read(operation_id=OPERATION_ID)
    assert outcome == "completed"
    assert calls == [("forward", OPERATION_ID)]
    assert current.recovery_generation == 0


@pytest.mark.asyncio
async def test_new_helper_takeover_increments_generation_once(tmp_path: Path) -> None:
    document = write_handoff(tmp_path)
    store = journal(tmp_path)
    store.start_v2(document=document, coordinator_container_id=OLD_COORDINATOR_ID)
    engine = FakeEngine([helper_container(), source_container()])
    service, calls = coordinator(tmp_path, engine=engine, journal=store)

    await service.run_once()

    current = store.read(operation_id=OPERATION_ID)
    assert calls == [("forward", OPERATION_ID)]
    assert current.coordinator_container_id == COORDINATOR_ID
    assert current.recovery_generation == 1


@pytest.mark.asyncio
async def test_commit_requested_starts_matched_candidate_and_routes_forward(
    tmp_path: Path,
) -> None:
    engine = FakeEngine([helper_container(), source_container()])
    advance_to_commit_requested(tmp_path, engine)
    service, calls = coordinator(
        tmp_path,
        engine=engine,
        journal=journal(tmp_path),
    )

    outcome = await service.run_once()

    assert outcome == "completed"
    assert engine.started == [CANDIDATE_ID]
    assert calls == [("forward", OPERATION_ID)]


@pytest.mark.asyncio
async def test_commit_requested_without_pending_still_reconciles_by_token_hash(
    tmp_path: Path,
) -> None:
    engine = FakeEngine([helper_container(), source_container()])
    advance_to_commit_requested(tmp_path, engine)
    (tmp_path / "update" / "pending.json").unlink()
    service, calls = coordinator(
        tmp_path,
        engine=engine,
        journal=journal(tmp_path),
    )

    outcome = await service.run_once()

    assert outcome == "completed"
    assert calls == [("forward", OPERATION_ID)]
    assert engine.started == [CANDIDATE_ID]


@pytest.mark.asyncio
async def test_commit_requested_fails_closed_when_candidate_does_not_start(
    tmp_path: Path,
) -> None:
    engine = FakeEngine([helper_container(), source_container()])
    engine.start_effective = False
    advance_to_commit_requested(tmp_path, engine)
    service, calls = coordinator(
        tmp_path,
        engine=engine,
        journal=journal(tmp_path),
    )

    outcome = await service.run_once()

    assert outcome == "manual_recovery"
    assert engine.started == [CANDIDATE_ID]
    assert calls == []
    assert engine.updated == [COORDINATOR_ID]


@pytest.mark.asyncio
async def test_success_result_reenters_forward_only_for_idempotent_cleanup(
    tmp_path: Path,
) -> None:
    engine = FakeEngine([helper_container(), source_container()])
    advance_to_commit_requested(tmp_path, engine)
    store = journal(tmp_path)
    store.checkpoint_v2(
        operation_id=OPERATION_ID,
        status="success",
        checkpoint="commit_requested",
    )
    service, calls = coordinator(tmp_path, engine=engine, journal=store)

    outcome = await service.run_once()

    assert outcome == "completed"
    assert calls == [("forward", OPERATION_ID)]
    assert engine.started == []


@pytest.mark.asyncio
async def test_recovery_generation_limit_fails_closed_and_disarms(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    store = journal(tmp_path)
    store.start_v2(document=document, coordinator_container_id=OLD_COORDINATOR_ID)
    for value in ("4" * 64, "5" * 64, "6" * 64):
        store.takeover_v2(
            operation_id=OPERATION_ID,
            coordinator_container_id=value,
        )
    engine = FakeEngine([helper_container(), source_container()])
    service, calls = coordinator(tmp_path, engine=engine, journal=store)

    outcome = await service.run_once()

    current = store.read(operation_id=OPERATION_ID)
    assert outcome == "manual_recovery"
    assert calls == []
    assert current.status == "rollback_failed"
    assert current.error_code == "recovery_generation_exhausted"
    assert engine.updated == [COORDINATOR_ID]


@pytest.mark.asyncio
async def test_disarm_failure_keeps_helper_alive_for_retry(tmp_path: Path) -> None:
    write_handoff(tmp_path)
    engine = FakeEngine([helper_container(), source_container()])
    engine.fail_disarm = True
    lock = FakeLock()
    service, _calls = coordinator(
        tmp_path,
        engine=engine,
        journal=journal(tmp_path),
        lock=lock,
    )

    outcome = await service.run_once()

    assert outcome == "waiting_for_disarm"
    assert lock.released is True
    assert engine.containers[COORDINATOR_ID]["HostConfig"]["RestartPolicy"][
        "Name"
    ] == "unless-stopped"


@pytest.mark.asyncio
async def test_cleanup_removes_only_stopped_disarmed_strict_helper(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    valid = helper_container(running=False, policy="no")
    invalid = helper_container("7" * 64, running=False, policy="no")
    invalid["Config"]["Cmd"] = ["sh"]
    engine = FakeEngine([valid, invalid])

    removed = await ExitedUpdaterCleanupService(
        engine=engine,
        socket_path=SOCKET_PATH,
    ).cleanup(document=document)

    assert removed == (COORDINATOR_ID,)
    assert engine.removed == [COORDINATOR_ID]
    assert "7" * 64 in engine.containers


def test_cleanup_observer_caches_handoff_before_terminal_marker_cleanup(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    pending = tmp_path / "update" / "pending.json"
    UpdaterCandidateService(pending_path=pending).prepare(document)
    engine = FakeEngine([helper_container(running=False, policy="no")])
    observer = ExitedUpdaterCleanupObserver(
        cleanup_service=ExitedUpdaterCleanupService(
            engine=engine,
            socket_path=SOCKET_PATH,
        ),
        data_directory=tmp_path,
        pending_path=pending,
    )

    removed = observer.observe()

    assert removed == (COORDINATOR_ID,)
    assert engine.removed == [COORDINATOR_ID]
