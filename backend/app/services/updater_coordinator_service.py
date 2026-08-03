from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from app.services.update_execution_gate import UpdateExecutionGate
from app.services.update_snapshot_service import (
    MAX_RECOVERY_GENERATION,
    HandoffDocument,
    UpdaterResult,
    UpdaterResultJournal,
    UpdaterResultV2,
    UpdateSnapshotError,
    read_handoff,
)
from app.services.updater_candidate_service import UpdaterCandidateService
from app.services.updater_handoff_service import (
    UPDATE_OPERATION_LABEL,
    UPDATE_ROLE_LABEL,
    UPDATER_COMMAND,
    UPDATER_ROLE,
    validate_operation_id,
)
from app.services.updater_process_lock import (
    UpdaterProcessLock,
    UpdaterProcessLockError,
)
from app.services.updater_recovery_decision_service import (
    UpdaterDockerIdentityService,
    decide_recovery,
)
from app.services.updater_state_machine import UpdaterStateMachineError

CoordinatorOutcome = Literal[
    "completed",
    "waiting_for_lock",
    "waiting_for_disarm",
    "manual_recovery",
]
CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HOSTNAME_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
TERMINAL_STATUSES = frozenset(
    {"success", "failed", "rolled_back", "rollback_failed"}
)


class CoordinatorEngine(Protocol):
    async def list_containers(self) -> list[dict[str, Any]]: ...

    async def inspect_container(self, container_id: str) -> dict[str, Any]: ...

    async def update_restart_policy(
        self,
        container_id: str,
        *,
        restart_policy: dict[str, Any],
    ) -> None: ...

    async def start_container(self, container_id: str) -> None: ...

    async def remove_container(self, container_id: str) -> None: ...


class UpdateExecutor(Protocol):
    async def execute(self, *, operation_id: str) -> Any: ...


ExecutorFactory = Callable[[str], UpdateExecutor]
LockFactory = Callable[[], UpdaterProcessLock]


@dataclass(frozen=True, slots=True)
class CoordinatorIdentity:
    container_id: str
    container: dict[str, Any]


class UpdaterCoordinator:
    """在持有全局锁时编排 updater v2 执行器。"""

    def __init__(
        self,
        *,
        engine: CoordinatorEngine,
        data_directory: Path,
        pending_path: Path,
        socket_path: str,
        hostname: str,
        operation_id: str,
        journal: UpdaterResultJournal,
        candidate_service: UpdaterCandidateService,
        forward_factory: ExecutorFactory,
        rollback_factory: ExecutorFactory,
        lock_factory: LockFactory | None = None,
    ) -> None:
        validate_operation_id(operation_id)
        normalized_hostname = hostname.strip().lower()
        if not HOSTNAME_PATTERN.fullmatch(normalized_hostname):
            raise ValueError("updater hostname 不是有效容器短标识")
        self._engine = engine
        self._data_directory = Path(data_directory)
        self._pending_path = Path(pending_path)
        self._socket_path = socket_path
        self._hostname = normalized_hostname
        self._operation_id = operation_id
        self._journal = journal
        self._candidate_service = candidate_service
        self._forward_factory = forward_factory
        self._rollback_factory = rollback_factory
        self._lock_factory = lock_factory or (
            lambda: UpdaterProcessLock(
                self._data_directory / "update" / "updater.lock"
            )
        )

    async def run_once(self) -> CoordinatorOutcome:
        lock = self._lock_factory()
        try:
            lock.acquire()
        except UpdaterProcessLockError:
            return "waiting_for_lock"
        try:
            return await self._run_locked()
        finally:
            lock.release()

    async def _run_locked(self) -> CoordinatorOutcome:
        document = read_handoff(
            self._handoff_path(),
            expected_operation_id=self._operation_id,
        )
        identity = await identify_current_coordinator(
            engine=self._engine,
            document=document,
            hostname=self._hostname,
            socket_path=self._socket_path,
        )
        result = self._read_optional_result()

        if result is None:
            await self._forward_factory(identity.container_id).execute(
                operation_id=self._operation_id
            )
            return await self._finish(identity.container_id)
        if isinstance(result, UpdaterResult):
            return await self._manual(identity.container_id)
        if result.status == "success":
            await self._forward_factory(identity.container_id).execute(
                operation_id=self._operation_id
            )
            return await self._finish(identity.container_id)
        if result.status in TERMINAL_STATUSES:
            return await self._finish(identity.container_id)

        marker = UpdateExecutionGate(
            pending_path=str(self._pending_path)
        ).read_pending_marker()
        if isinstance(marker, str):
            return await self._manual(identity.container_id)
        observation = await UpdaterDockerIdentityService(
            engine=self._engine,
            socket_path=self._socket_path,
        ).observe(
            document=document,
            result=result,
            marker=marker,
            hostname=self._hostname,
        )
        if observation.coordinator.status != "matched":
            return await self._manual(identity.container_id)

        if result.coordinator_container_id != identity.container_id:
            try:
                result = self._journal.takeover_v2(
                    operation_id=self._operation_id,
                    coordinator_container_id=identity.container_id,
                )
            except UpdateSnapshotError:
                if result.recovery_generation < MAX_RECOVERY_GENERATION:
                    raise
                self._mark_recovery_exhausted(document, result)
                return await self._manual(identity.container_id)

        decision = decide_recovery(result=result, observation=observation)
        if decision.action in {"continue_forward", "cleanup_success"}:
            await self._forward_factory(identity.container_id).execute(
                operation_id=self._operation_id
            )
        elif decision.action in {"begin_rollback", "continue_rollback"}:
            await self._rollback_factory(identity.container_id).execute(
                operation_id=self._operation_id
            )
        elif decision.action == "reconcile_commit":
            if observation.candidate.container_id is None:
                return await self._manual(identity.container_id)
            if observation.candidate.running is not True:
                await self._engine.start_container(
                    observation.candidate.container_id
                )
                observation = await UpdaterDockerIdentityService(
                    engine=self._engine,
                    socket_path=self._socket_path,
                ).observe(
                    document=document,
                    result=result,
                    marker=marker,
                    hostname=self._hostname,
                )
                if (
                    observation.candidate.status != "matched"
                    or observation.candidate.container_id
                    != result.candidate_container_id
                    or observation.candidate.running is not True
                ):
                    return await self._manual(identity.container_id)
            await self._forward_factory(identity.container_id).execute(
                operation_id=self._operation_id
            )
        elif decision.action == "await_rollback_reconciliation":
            pass
        else:
            return await self._manual(identity.container_id)
        return await self._finish(identity.container_id)

    async def _finish(self, coordinator_id: str) -> CoordinatorOutcome:
        if await self._disarm(coordinator_id):
            return "completed"
        return "waiting_for_disarm"

    async def _manual(self, coordinator_id: str) -> CoordinatorOutcome:
        if not await self._disarm(coordinator_id):
            return "waiting_for_disarm"
        return "manual_recovery"

    async def _disarm(self, coordinator_id: str) -> bool:
        try:
            await self._engine.update_restart_policy(
                coordinator_id,
                restart_policy={"Name": "no", "MaximumRetryCount": 0},
            )
            container = await self._engine.inspect_container(coordinator_id)
        except Exception:
            return False
        host = container.get("HostConfig")
        policy = host.get("RestartPolicy") if isinstance(host, dict) else None
        return (
            isinstance(policy, dict)
            and policy.get("Name") == "no"
            and policy.get("MaximumRetryCount", 0) == 0
        )

    def _mark_recovery_exhausted(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
    ) -> None:
        if result.status == "commit_requested":
            return
        try:
            if result.status != "rolling_back":
                preparation = self._candidate_service.prepare(document)
                token_hash = "sha256:" + hashlib.sha256(
                    preparation.marker.candidate_token.encode()
                ).hexdigest()
                result = self._journal.checkpoint_v2(
                    operation_id=self._operation_id,
                    status="rolling_back",
                    checkpoint="rollback_started",
                    candidate_token_hash=token_hash,
                    candidate_container_id=result.candidate_container_id,
                    rollback_started=True,
                )
            self._journal.checkpoint_v2(
                operation_id=self._operation_id,
                status="rollback_failed",
                checkpoint=result.checkpoint,
                error_code="recovery_generation_exhausted",
                public_error_message="更新恢复次数已达上限，需要人工恢复",
            )
        except Exception:
            return

    def _read_optional_result(self) -> UpdaterResult | UpdaterResultV2 | None:
        path = self._journal.directory / f"{self._operation_id}.json"
        if not path.exists() and not path.is_symlink():
            return None
        return self._journal.read(operation_id=self._operation_id)

    def _handoff_path(self) -> Path:
        return (
            self._data_directory
            / "update"
            / "operations"
            / f"{self._operation_id}.handoff.json"
        )


async def identify_current_coordinator(
    *,
    engine: CoordinatorEngine,
    document: HandoffDocument,
    hostname: str,
    socket_path: str,
) -> CoordinatorIdentity:
    summaries = await engine.list_containers()
    candidates: list[str] = []
    for summary in summaries:
        container_id = summary.get("Id")
        labels = summary.get("Labels")
        if (
            isinstance(container_id, str)
            and CONTAINER_ID_PATTERN.fullmatch(container_id)
            and container_id.startswith(hostname)
            and isinstance(labels, dict)
            and labels.get(UPDATE_ROLE_LABEL) == UPDATER_ROLE
            and labels.get(UPDATE_OPERATION_LABEL) == document.operation_id
        ):
            candidates.append(container_id)
    if len(candidates) != 1:
        raise UpdaterStateMachineError("无法唯一识别当前 updater 容器")
    container = await engine.inspect_container(candidates[0])
    if not coordinator_identity_matches(
        container,
        document=document,
        socket_path=socket_path,
    ):
        raise UpdaterStateMachineError("当前 updater 容器身份不匹配")
    return CoordinatorIdentity(candidates[0], container)


def coordinator_identity_matches(
    container: dict[str, Any],
    *,
    document: HandoffDocument,
    socket_path: str,
) -> bool:
    config = container.get("Config")
    host = container.get("HostConfig")
    mounts = container.get("Mounts")
    if not isinstance(config, dict) or not isinstance(host, dict):
        return False
    labels = config.get("Labels")
    environment = _environment(config.get("Env"))
    targets = {
        item.get("Destination")
        for item in mounts
        if isinstance(item, dict) and isinstance(item.get("Destination"), str)
    } if isinstance(mounts, list) else set()
    data_mount = next(
        (mount for mount in document.candidate.mounts if mount.target == "/data"),
        None,
    )
    actual_data = next(
        (
            item
            for item in mounts
            if isinstance(item, dict) and item.get("Destination") == "/data"
        ),
        None,
    ) if isinstance(mounts, list) else None
    return (
        isinstance(labels, dict)
        and labels.get(UPDATE_ROLE_LABEL) == UPDATER_ROLE
        and labels.get(UPDATE_OPERATION_LABEL) == document.operation_id
        and environment.get("MEDIASYNC_UPDATE_OPERATION_ID") == document.operation_id
        and config.get("Cmd") == UPDATER_COMMAND
        and config.get("Image") == document.target_image
        and host.get("NetworkMode") == "none"
        and host.get("ReadonlyRootfs") is True
        and targets == {"/data", socket_path}
        and data_mount is not None
        and isinstance(actual_data, dict)
        and actual_data.get("Type") == data_mount.type
        and _mount_source(actual_data, data_mount.type) == data_mount.source
        and actual_data.get("RW") is True
    )


class ExitedUpdaterCleanupService:
    def __init__(self, *, engine: CoordinatorEngine, socket_path: str) -> None:
        self._engine = engine
        self._socket_path = socket_path

    async def cleanup(self, *, document: HandoffDocument) -> tuple[str, ...]:
        removed: list[str] = []
        summaries = await self._engine.list_containers()
        for summary in summaries:
            container_id = summary.get("Id")
            labels = summary.get("Labels")
            if (
                not isinstance(container_id, str)
                or not CONTAINER_ID_PATTERN.fullmatch(container_id)
                or not isinstance(labels, dict)
                or labels.get(UPDATE_ROLE_LABEL) != UPDATER_ROLE
                or labels.get(UPDATE_OPERATION_LABEL) != document.operation_id
            ):
                continue
            container = await self._engine.inspect_container(container_id)
            state = container.get("State")
            host = container.get("HostConfig")
            policy = host.get("RestartPolicy") if isinstance(host, dict) else None
            if (
                coordinator_identity_matches(
                    container,
                    document=document,
                    socket_path=self._socket_path,
                )
                and isinstance(state, dict)
                and state.get("Running") is False
                and isinstance(policy, dict)
                and policy.get("Name") == "no"
            ):
                await self._engine.remove_container(container_id)
                removed.append(container_id)
        return tuple(removed)


def _environment(value: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(value, list):
        return result
    for item in value:
        if isinstance(item, str):
            key, separator, data = item.partition("=")
            if separator and key not in result:
                result[key] = data
    return result


def _mount_source(mount: dict[str, Any], mount_type: str) -> object:
    return mount.get("Name") if mount_type == "volume" else mount.get("Source")
