from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.services.docker_capability_service import (
    APPLIANCE_COMMAND,
    CONTAINER_ID_PATTERN,
)
from app.services.update_execution_gate import PendingUpdateMarker
from app.services.update_snapshot_service import (
    HandoffDocument,
    UpdaterResult,
    UpdaterResultV2,
)
from app.services.updater_handoff_service import (
    CANDIDATE_ROLE,
    UPDATE_OPERATION_LABEL,
    UPDATE_ROLE_LABEL,
    UPDATER_COMMAND,
    UPDATER_ROLE,
)

IdentityStatus = Literal["missing", "matched", "conflict"]
RecoveryAction = Literal[
    "start_new",
    "continue_forward",
    "begin_rollback",
    "continue_rollback",
    "reconcile_commit",
    "cleanup_success",
    "await_rollback_reconciliation",
    "manual_recovery",
]


class UpdaterRecoveryObservationError(RuntimeError):
    """无法安全读取 Docker 恢复现场。"""


class RecoveryDockerEngine(Protocol):
    async def list_containers(self) -> list[dict[str, Any]]: ...

    async def inspect_container(self, container_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ContainerIdentityObservation:
    status: IdentityStatus
    container_id: str | None = None
    running: bool | None = None
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class DockerRecoveryObservation:
    source: ContainerIdentityObservation
    candidate: ContainerIdentityObservation
    coordinator: ContainerIdentityObservation


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    action: RecoveryAction
    automatic: bool
    reason_code: str


class UpdaterDockerIdentityService:
    """只读发现并严格验证一次更新操作关联的 Docker 容器。"""

    def __init__(
        self,
        *,
        engine: RecoveryDockerEngine,
        socket_path: str,
    ) -> None:
        self._engine = engine
        self._socket_path = socket_path

    async def observe(
        self,
        *,
        document: HandoffDocument,
        result: UpdaterResultV2,
        marker: PendingUpdateMarker | None,
        hostname: str,
    ) -> DockerRecoveryObservation:
        if document.operation_id != result.operation_id:
            raise UpdaterRecoveryObservationError("恢复文档与结果操作标识不匹配")
        try:
            summaries = await self._engine.list_containers()
        except Exception as exc:
            raise UpdaterRecoveryObservationError(
                "无法读取 Docker 容器列表"
            ) from exc
        summary_ids = _summary_ids(summaries)
        relevant_ids = {
            container_id
            for container_id in summary_ids
            if container_id in {result.source_container_id, result.candidate_container_id}
            or _summary_has_operation(summaries, container_id, result.operation_id)
        }
        containers: list[dict[str, Any]] = []
        for container_id in sorted(relevant_ids):
            try:
                containers.append(await self._engine.inspect_container(container_id))
            except Exception as exc:
                raise UpdaterRecoveryObservationError(
                    "无法读取更新关联容器信息"
                ) from exc

        source = _observe_source(containers, document=document, result=result)
        candidate = _observe_candidate(
            containers,
            document=document,
            result=result,
            marker=marker,
        )
        coordinator = _observe_coordinator(
            containers,
            document=document,
            result=result,
            hostname=hostname,
            socket_path=self._socket_path,
        )
        return DockerRecoveryObservation(
            source=source,
            candidate=candidate,
            coordinator=coordinator,
        )


def decide_recovery(
    *,
    result: UpdaterResult | UpdaterResultV2 | None,
    observation: DockerRecoveryObservation,
) -> RecoveryDecision:
    """根据持久状态与已验证 Docker 现场返回唯一且无副作用的决策。"""
    if observation.coordinator.status != "matched":
        return _manual(observation.coordinator.reason_code or "coordinator_unresolved")
    for identity in (observation.source, observation.candidate):
        if identity.status == "conflict":
            return _manual(identity.reason_code or "container_identity_conflict")

    if result is None:
        if observation.source.status == "matched" and observation.source.running:
            return RecoveryDecision("start_new", True, "new_operation_safe")
        return _manual("source_not_ready_for_new_operation")

    if isinstance(result, UpdaterResult):
        return _decide_v1(result, observation)

    if result.status in {"failed", "rollback_failed"}:
        return _manual(f"terminal_{result.status}")
    if result.status == "success":
        return RecoveryDecision("cleanup_success", True, "success_cleanup")
    if result.status == "rolled_back":
        return RecoveryDecision(
            "await_rollback_reconciliation",
            True,
            "rolled_back_waiting_for_appliance",
        )
    if result.status == "commit_requested":
        if observation.candidate.status != "matched":
            return _manual("commit_candidate_unresolved")
        return RecoveryDecision(
            "reconcile_commit",
            True,
            "commit_requires_database_reconciliation",
        )
    if observation.source.status != "matched":
        return _manual("source_container_unresolved")
    if result.status == "rolling_back":
        return RecoveryDecision(
            "continue_rollback",
            True,
            "rollback_checkpoint_recovery",
        )
    if result.status in {"switching", "verifying"}:
        return RecoveryDecision(
            "begin_rollback",
            True,
            "forward_switch_interrupted",
        )
    if result.status == "snapshotting":
        if observation.source.running:
            return RecoveryDecision(
                "continue_forward",
                True,
                "source_still_running",
            )
        return RecoveryDecision(
            "begin_rollback",
            True,
            "source_stopped_before_commit",
        )
    return _manual("unsupported_recovery_state")


def _decide_v1(
    result: UpdaterResult,
    observation: DockerRecoveryObservation,
) -> RecoveryDecision:
    if result.status == "success":
        return RecoveryDecision("cleanup_success", True, "v1_success_cleanup")
    if result.status == "rolled_back":
        return RecoveryDecision(
            "await_rollback_reconciliation",
            True,
            "v1_rolled_back_waiting_for_appliance",
        )
    if result.status == "commit_requested" and observation.candidate.status == "matched":
        return RecoveryDecision(
            "reconcile_commit",
            True,
            "v1_commit_compatibility",
        )
    return _manual("v1_state_requires_manual_recovery")


def _manual(reason_code: str) -> RecoveryDecision:
    return RecoveryDecision("manual_recovery", False, reason_code)


def _summary_ids(summaries: list[dict[str, Any]]) -> set[str]:
    identifiers: set[str] = set()
    for summary in summaries:
        container_id = summary.get("Id")
        if isinstance(container_id, str) and len(container_id) == 64:
            if CONTAINER_ID_PATTERN.fullmatch(container_id):
                identifiers.add(container_id)
    return identifiers


def _summary_has_operation(
    summaries: list[dict[str, Any]],
    container_id: str,
    operation_id: str,
) -> bool:
    for summary in summaries:
        if summary.get("Id") != container_id:
            continue
        labels = summary.get("Labels")
        return isinstance(labels, dict) and labels.get(UPDATE_OPERATION_LABEL) == operation_id
    return False


def _observe_source(
    containers: list[dict[str, Any]],
    *,
    document: HandoffDocument,
    result: UpdaterResultV2,
) -> ContainerIdentityObservation:
    matches = [item for item in containers if item.get("Id") == result.source_container_id]
    if not matches:
        return ContainerIdentityObservation("missing", reason_code="source_missing")
    if len(matches) != 1 or not _valid_source(matches[0], document, result):
        return ContainerIdentityObservation("conflict", reason_code="source_identity_conflict")
    return _matched(matches[0])


def _observe_candidate(
    containers: list[dict[str, Any]],
    *,
    document: HandoffDocument,
    result: UpdaterResultV2,
    marker: PendingUpdateMarker | None,
) -> ContainerIdentityObservation:
    labeled = [
        item
        for item in containers
        if _labels(item).get(UPDATE_OPERATION_LABEL) == result.operation_id
        and _labels(item).get(UPDATE_ROLE_LABEL) == CANDIDATE_ROLE
    ]
    if result.candidate_container_id is not None:
        identified = [
            item
            for item in containers
            if item.get("Id") == result.candidate_container_id
        ]
        if not identified and not labeled:
            return ContainerIdentityObservation("missing", reason_code="candidate_missing")
        if len(identified) != 1 or identified[0] not in labeled:
            return ContainerIdentityObservation(
                "conflict",
                reason_code="candidate_identity_conflict",
            )
        selected = identified
    else:
        selected = labeled
    if not selected:
        return ContainerIdentityObservation("missing", reason_code="candidate_not_created")
    if len(selected) != 1 or len(labeled) != 1:
        return ContainerIdentityObservation(
            "conflict",
            reason_code="candidate_not_unique",
        )
    candidate = selected[0]
    if not _valid_candidate(candidate, document, result, marker):
        return ContainerIdentityObservation(
            "conflict",
            reason_code="candidate_identity_conflict",
        )
    return _matched(candidate)


def _observe_coordinator(
    containers: list[dict[str, Any]],
    *,
    document: HandoffDocument,
    result: UpdaterResultV2,
    hostname: str,
    socket_path: str,
) -> ContainerIdentityObservation:
    normalized_hostname = hostname.strip().lower()
    if not CONTAINER_ID_PATTERN.fullmatch(normalized_hostname):
        return ContainerIdentityObservation(
            "conflict",
            reason_code="coordinator_hostname_invalid",
        )
    operation_containers = [
        item
        for item in containers
        if _labels(item).get(UPDATE_OPERATION_LABEL) == result.operation_id
    ]
    prefix_matches = [
        item
        for item in operation_containers
        if isinstance(item.get("Id"), str)
        and item["Id"].startswith(normalized_hostname)
    ]
    if len(prefix_matches) != 1:
        return ContainerIdentityObservation(
            "conflict" if prefix_matches or operation_containers else "missing",
            reason_code="coordinator_not_unique",
        )
    coordinator = prefix_matches[0]
    if (
        _labels(coordinator).get(UPDATE_ROLE_LABEL) != UPDATER_ROLE
        or not _valid_coordinator(coordinator, document, result, socket_path)
    ):
        return ContainerIdentityObservation(
            "conflict",
            reason_code="coordinator_identity_conflict",
        )
    return _matched(coordinator)


def _valid_source(
    container: dict[str, Any],
    document: HandoffDocument,
    result: UpdaterResultV2,
) -> bool:
    return (
        container.get("Id") == document.current_container_id == result.source_container_id
        and container.get("Image") == document.source_image_id == result.source_image_id
        and _name(container) == document.candidate.name == result.source_container_name
        and _mount_matches(container, document=document, target="/data")
    )


def _valid_candidate(
    container: dict[str, Any],
    document: HandoffDocument,
    result: UpdaterResultV2,
    marker: PendingUpdateMarker | None,
) -> bool:
    if marker is None or marker.operation_id != result.operation_id:
        return False
    expected_hash = "sha256:" + hashlib.sha256(marker.candidate_token.encode()).hexdigest()
    if result.candidate_token_hash != expected_hash:
        return False
    return (
        _name(container) == result.source_container_name
        and _config(container).get("Image") == result.target_image == document.target_image
        and _config(container).get("Cmd") == APPLIANCE_COMMAND
        and _environment(container).get("MEDIASYNC_CANDIDATE_TOKEN")
        == marker.candidate_token
        and _mount_matches(container, document=document, target="/data")
    )


def _valid_coordinator(
    container: dict[str, Any],
    document: HandoffDocument,
    result: UpdaterResultV2,
    socket_path: str,
) -> bool:
    host = _mapping(container.get("HostConfig"))
    mounts = container.get("Mounts")
    targets = {
        item.get("Destination")
        for item in mounts
        if isinstance(item, dict) and isinstance(item.get("Destination"), str)
    } if isinstance(mounts, list) else set()
    return (
        _config(container).get("Cmd") == UPDATER_COMMAND
        and _config(container).get("Image") == result.target_image == document.target_image
        and host.get("NetworkMode") == "none"
        and host.get("ReadonlyRootfs") is True
        and targets == {"/data", socket_path}
        and _mount_matches(container, document=document, target="/data")
    )


def _mount_matches(
    container: dict[str, Any],
    *,
    document: HandoffDocument,
    target: str,
) -> bool:
    expected = next(
        (mount for mount in document.candidate.mounts if mount.target == target),
        None,
    )
    mounts = container.get("Mounts")
    if expected is None or not isinstance(mounts, list):
        return False
    matched = [
        item
        for item in mounts
        if isinstance(item, dict) and item.get("Destination") == target
    ]
    return len(matched) == 1 and (
        matched[0].get("Type") == expected.type
        and _mount_source(matched[0], expected.type) == expected.source
        and matched[0].get("RW") is (not expected.read_only)
    )


def _mount_source(mount: dict[str, Any], mount_type: str) -> object:
    if mount_type == "volume":
        return mount.get("Name")
    return mount.get("Source")


def _matched(container: dict[str, Any]) -> ContainerIdentityObservation:
    state = _mapping(container.get("State"))
    return ContainerIdentityObservation(
        "matched",
        container_id=container.get("Id"),
        running=state.get("Running") is True,
    )


def _name(container: dict[str, Any]) -> str:
    value = container.get("Name")
    return value.removeprefix("/") if isinstance(value, str) else ""


def _labels(container: dict[str, Any]) -> dict[str, Any]:
    return _mapping(_config(container).get("Labels"))


def _config(container: dict[str, Any]) -> dict[str, Any]:
    return _mapping(container.get("Config"))


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _environment(container: dict[str, Any]) -> dict[str, str]:
    values = _config(container).get("Env")
    if not isinstance(values, list):
        return {}
    result: dict[str, str] = {}
    for item in values:
        if isinstance(item, str):
            key, separator, value = item.partition("=")
            if separator and key not in result:
                result[key] = value
    return result
