from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.update_execution_gate import PendingUpdateMarker
from app.services.update_snapshot_service import (
    HandoffCandidate,
    HandoffDocument,
    HandoffMount,
    UpdaterResult,
    UpdaterResultV2,
)
from app.services.updater_recovery_decision_service import (
    ContainerIdentityObservation,
    DockerRecoveryObservation,
    UpdaterDockerIdentityService,
    decide_recovery,
)

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
SOURCE_ID = "a" * 64
CANDIDATE_ID = "b" * 64
COORDINATOR_ID = "c" * 64
SOURCE_IMAGE_ID = f"sha256:{'d' * 64}"
TARGET_DIGEST = f"sha256:{'e' * 64}"
TARGET_IMAGE = f"josephyjq/mediasync@{TARGET_DIGEST}"
TARGET_REVISION = "f" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"
CANDIDATE_TOKEN_HASH = (
    "sha256:" + hashlib.sha256(CANDIDATE_TOKEN.encode()).hexdigest()
)
SOCKET_PATH = "/var/run/docker.sock"


def handoff() -> HandoffDocument:
    return HandoffDocument(
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
        candidate=HandoffCandidate(
            container_id=SOURCE_ID,
            name="MediaSync",
            env=("TZ=Asia/Shanghai",),
            user="1000:1000",
            labels={},
            mounts=(HandoffMount(
                type="volume",
                source="mediasync-data",
                target="/data",
                read_only=False,
            ),),
            exposed_ports=("9090/tcp",),
            port_bindings={},
            restart_policy={"Name": "unless-stopped", "MaximumRetryCount": 0},
            network_mode="bridge",
            networks={"bridge": ("MediaSync",)},
            dns=(),
            group_add=(),
            readonly_rootfs=False,
        ),
    )


def result(
    *,
    status: str = "switching",
    checkpoint: str = "candidate_created",
    candidate_id: str | None = CANDIDATE_ID,
) -> UpdaterResultV2:
    return UpdaterResultV2(
        schema_version=2,
        operation_id=OPERATION_ID,
        sequence=7,
        status=status,
        checkpoint=checkpoint,
        recovery_generation=0,
        coordinator_container_id=COORDINATOR_ID,
        source_container_id=SOURCE_ID,
        source_image_id=SOURCE_IMAGE_ID,
        source_container_name="MediaSync",
        target_image=TARGET_IMAGE,
        target_revision=TARGET_REVISION,
        candidate_token_hash=CANDIDATE_TOKEN_HASH,
        candidate_container_id=candidate_id,
        rollback_started=False,
        updated_at=datetime(2026, 8, 3, tzinfo=UTC),
    )


def marker() -> PendingUpdateMarker:
    return PendingUpdateMarker(
        operation_id=OPERATION_ID,
        target_version="v0.3.0-rc.1",
        target_digest=TARGET_DIGEST,
        target_revision=TARGET_REVISION,
        candidate_token=CANDIDATE_TOKEN,
    )


def source_container(
    *,
    running: bool = False,
    name: str = "mediasync-previous-12345678",
) -> dict[str, Any]:
    return {
        "Id": SOURCE_ID,
        "Name": f"/{name}",
        "Image": SOURCE_IMAGE_ID,
        "Config": {"Image": "josephyjq/mediasync:v0.2.0-rc.9"},
        "State": {"Running": running},
        "Mounts": [{
            "Type": "volume",
            "Name": "mediasync-data",
            "Source": "/var/lib/docker/volumes/mediasync-data/_data",
            "Destination": "/data",
            "RW": True,
        }],
    }


def candidate_container(
    *,
    container_id: str = CANDIDATE_ID,
    token: str = CANDIDATE_TOKEN,
) -> dict[str, Any]:
    return {
        "Id": container_id,
        "Name": "/MediaSync",
        "Image": f"sha256:{'1' * 64}",
        "Config": {
            "Image": TARGET_IMAGE,
            "Cmd": ["python", "-m", "app.appliance"],
            "Env": [f"MEDIASYNC_CANDIDATE_TOKEN={token}"],
            "Labels": {
                "io.mediasync.update.role": "candidate",
                "io.mediasync.update.operation": OPERATION_ID,
            },
        },
        "State": {"Running": True},
        "Mounts": [{
            "Type": "volume",
            "Name": "mediasync-data",
            "Source": "/var/lib/docker/volumes/mediasync-data/_data",
            "Destination": "/data",
            "RW": True,
        }],
    }


def coordinator_container(
    *,
    container_id: str = COORDINATOR_ID,
) -> dict[str, Any]:
    return {
        "Id": container_id,
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
        "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True},
        "State": {"Running": True},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "mediasync-data",
                "Destination": "/data",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": SOCKET_PATH,
                "Destination": SOCKET_PATH,
                "RW": True,
            },
        ],
    }


class FakeEngine:
    def __init__(self, containers: list[dict[str, Any]]) -> None:
        self.containers = containers
        self.inspected: list[str] = []

    async def list_containers(self) -> list[dict[str, Any]]:
        return [
            {"Id": item["Id"], "Labels": item.get("Config", {}).get("Labels", {})}
            for item in self.containers
        ]

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        self.inspected.append(container_id)
        return next(item for item in self.containers if item["Id"] == container_id)


@pytest.mark.asyncio
async def test_docker_identity_observation_strictly_matches_all_roles() -> None:
    engine = FakeEngine([
        source_container(),
        candidate_container(),
        coordinator_container(),
    ])

    observed = await UpdaterDockerIdentityService(
        engine=engine,
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.source == ContainerIdentityObservation(
        "matched", SOURCE_ID, False
    )
    assert observed.candidate == ContainerIdentityObservation(
        "matched", CANDIDATE_ID, True
    )
    assert observed.coordinator == ContainerIdentityObservation(
        "matched", COORDINATOR_ID, True
    )
    assert set(engine.inspected) == {SOURCE_ID, CANDIDATE_ID, COORDINATOR_ID}


@pytest.mark.asyncio
async def test_source_keeps_original_name_before_rename_checkpoint() -> None:
    current = result(
        status="snapshotting",
        checkpoint="pending_ready",
        candidate_id=None,
    )
    engine = FakeEngine([
        source_container(name="MediaSync", running=True),
        coordinator_container(),
    ])

    observed = await UpdaterDockerIdentityService(
        engine=engine,
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=current,
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.source.status == "matched"


@pytest.mark.asyncio
async def test_candidate_token_mismatch_fails_closed() -> None:
    observed = await UpdaterDockerIdentityService(
        engine=FakeEngine([
            source_container(),
            candidate_container(token="different-token-0123456789-abcdef"),
            coordinator_container(),
        ]),
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.candidate.status == "conflict"
    assert observed.candidate.reason_code == "candidate_identity_conflict"


@pytest.mark.asyncio
async def test_multiple_candidates_are_never_guessed() -> None:
    other_id = "9" * 64
    observed = await UpdaterDockerIdentityService(
        engine=FakeEngine([
            source_container(),
            candidate_container(),
            candidate_container(container_id=other_id),
            coordinator_container(),
        ]),
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.candidate.status == "conflict"
    assert observed.candidate.reason_code == "candidate_not_unique"


@pytest.mark.asyncio
async def test_expected_candidate_with_wrong_role_is_identity_conflict() -> None:
    candidate = candidate_container()
    candidate["Config"]["Labels"]["io.mediasync.update.role"] = "updater"

    observed = await UpdaterDockerIdentityService(
        engine=FakeEngine([
            source_container(),
            candidate,
            coordinator_container(),
        ]),
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.candidate.status == "conflict"
    assert observed.candidate.reason_code == "candidate_identity_conflict"


@pytest.mark.asyncio
async def test_current_coordinator_requires_unique_hostname_prefix() -> None:
    other_id = COORDINATOR_ID[:12] + "8" * 52
    observed = await UpdaterDockerIdentityService(
        engine=FakeEngine([
            source_container(),
            candidate_container(),
            coordinator_container(),
            coordinator_container(container_id=other_id),
        ]),
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.coordinator.status == "conflict"
    assert observed.coordinator.reason_code == "coordinator_not_unique"


@pytest.mark.asyncio
async def test_hostname_matching_wrong_role_is_coordinator_conflict() -> None:
    coordinator = coordinator_container()
    coordinator["Config"]["Labels"]["io.mediasync.update.role"] = "candidate"

    observed = await UpdaterDockerIdentityService(
        engine=FakeEngine([
            source_container(),
            candidate_container(),
            coordinator,
        ]),
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )

    assert observed.coordinator.status == "conflict"
    assert observed.coordinator.reason_code == "coordinator_identity_conflict"


def observation(
    *,
    source_status: str = "matched",
    source_running: bool = False,
    candidate_status: str = "matched",
    coordinator_status: str = "matched",
) -> DockerRecoveryObservation:
    return DockerRecoveryObservation(
        source=ContainerIdentityObservation(
            source_status,  # type: ignore[arg-type]
            SOURCE_ID if source_status == "matched" else None,
            source_running if source_status == "matched" else None,
            "source_identity_conflict" if source_status == "conflict" else None,
        ),
        candidate=ContainerIdentityObservation(
            candidate_status,  # type: ignore[arg-type]
            CANDIDATE_ID if candidate_status == "matched" else None,
            True if candidate_status == "matched" else None,
            "candidate_identity_conflict" if candidate_status == "conflict" else None,
        ),
        coordinator=ContainerIdentityObservation(
            coordinator_status,  # type: ignore[arg-type]
            COORDINATOR_ID if coordinator_status == "matched" else None,
            True if coordinator_status == "matched" else None,
            "coordinator_not_unique" if coordinator_status == "conflict" else None,
        ),
    )


@pytest.mark.parametrize(
    ("status", "checkpoint", "source_running", "action"),
    [
        ("snapshotting", "pending_ready", True, "continue_forward"),
        ("snapshotting", "pending_ready", False, "begin_rollback"),
        ("switching", "candidate_created", False, "begin_rollback"),
        ("verifying", "candidate_started", False, "begin_rollback"),
        ("commit_requested", "commit_requested", False, "reconcile_commit"),
    ],
)
def test_v2_recovery_decision_matrix(
    status: str,
    checkpoint: str,
    source_running: bool,
    action: str,
) -> None:
    current = result(
        status=status,
        checkpoint=checkpoint,
        candidate_id=None if checkpoint == "pending_ready" else CANDIDATE_ID,
    )

    decision = decide_recovery(
        result=current,
        observation=observation(source_running=source_running),
    )

    assert decision.action == action
    assert decision.automatic is True


def test_identity_conflict_always_requires_manual_recovery() -> None:
    decision = decide_recovery(
        result=result(),
        observation=observation(candidate_status="conflict"),
    )

    assert decision.action == "manual_recovery"
    assert decision.automatic is False
    assert decision.reason_code == "candidate_identity_conflict"


def test_v1_nonterminal_state_fails_closed() -> None:
    legacy = UpdaterResult(
        schema_version=1,
        operation_id=OPERATION_ID,
        sequence=2,
        status="switching",
        updated_at=datetime(2026, 8, 3, tzinfo=UTC),
    )

    decision = decide_recovery(result=legacy, observation=observation())

    assert decision.action == "manual_recovery"
    assert decision.reason_code == "v1_state_requires_manual_recovery"


@pytest.mark.asyncio
async def test_tampered_source_identity_prevents_automatic_recovery() -> None:
    container = source_container()
    container["Image"] = f"sha256:{'0' * 64}"
    engine = FakeEngine([
        container,
        candidate_container(),
        coordinator_container(),
    ])
    original = deepcopy(engine.containers)

    observed = await UpdaterDockerIdentityService(
        engine=engine,
        socket_path=SOCKET_PATH,
    ).observe(
        document=handoff(),
        result=result(),
        marker=marker(),
        hostname=COORDINATOR_ID[:12],
    )
    decision = decide_recovery(result=result(), observation=observed)

    assert engine.containers == original
    assert decision.action == "manual_recovery"
    assert decision.reason_code == "source_identity_conflict"
