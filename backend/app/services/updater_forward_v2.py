from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from app.services.update_execution_gate import PendingUpdateMarker
from app.services.update_snapshot_service import (
    HandoffDocument,
    UpdaterCheckpoint,
    UpdaterResultJournal,
    UpdaterResultV2,
    UpdaterStatus,
    UpdateSnapshotService,
    read_handoff,
)
from app.services.updater_candidate_service import (
    CandidatePreparation,
    UpdaterCandidateService,
)
from app.services.updater_recovery_decision_service import (
    UpdaterDockerIdentityService,
    source_identity_matches,
)
from app.services.updater_state_machine import (
    CandidateVerifier,
    CommitWaiter,
    UpdaterStateMachineError,
    previous_container_name,
)

FaultHook = Callable[[str], None]
CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UpdaterForwardEngine(Protocol):
    async def list_containers(self) -> list[dict[str, Any]]: ...

    async def inspect_container(self, container_id: str) -> dict[str, Any]: ...

    async def update_restart_policy(
        self,
        container_id: str,
        *,
        restart_policy: dict[str, Any],
    ) -> None: ...

    async def stop_container(self, container_id: str, *, timeout_seconds: int) -> None: ...

    async def wait_container(self, container_id: str) -> int: ...

    async def rename_container(self, container_id: str, *, name: str) -> None: ...

    async def create_container(self, *, name: str, config: dict[str, Any]) -> str: ...

    async def start_container(self, container_id: str) -> None: ...

    async def remove_container(self, container_id: str) -> None: ...


class UpdaterForwardV2:
    """只执行 v2 正常路径；失败后由独立回滚执行器接管。"""

    def __init__(
        self,
        *,
        engine: UpdaterForwardEngine,
        data_directory: Path,
        socket_path: str,
        coordinator_container_id: str,
        snapshot_service: UpdateSnapshotService,
        candidate_service: UpdaterCandidateService,
        journal: UpdaterResultJournal,
        verifier: CandidateVerifier,
        commit_waiter: CommitWaiter,
        stop_timeout_seconds: int = 90,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if not CONTAINER_ID_PATTERN.fullmatch(coordinator_container_id):
            raise ValueError("updater 协调器必须使用完整容器标识")
        if not 1 <= stop_timeout_seconds <= 600:
            raise ValueError("旧容器停止超时时间无效")
        self._engine = engine
        self._data_directory = Path(data_directory)
        self._socket_path = socket_path
        self._coordinator_container_id = coordinator_container_id
        self._snapshot_service = snapshot_service
        self._candidate_service = candidate_service
        self._journal = journal
        self._verifier = verifier
        self._commit_waiter = commit_waiter
        self._stop_timeout_seconds = stop_timeout_seconds
        self._fault_hook = fault_hook or (lambda _event: None)

    async def execute(self, *, operation_id: str) -> str:
        handoff_path = (
            self._data_directory
            / "update"
            / "operations"
            / f"{operation_id}.handoff.json"
        )
        document = read_handoff(
            handoff_path,
            expected_operation_id=operation_id,
        )
        result = self._load_or_start(document)
        self._validate_resumable(result)

        while result.status != "success":
            if result.checkpoint == "initialized":
                await self._fence_old_restart(document)
                result = self._checkpoint(
                    operation_id,
                    status="snapshotting",
                    checkpoint="old_restart_fenced",
                )
            elif result.checkpoint == "old_restart_fenced":
                await self._stop_old(document)
                result = self._checkpoint(
                    operation_id,
                    status="snapshotting",
                    checkpoint="old_stopped",
                )
            elif result.checkpoint == "old_stopped":
                self._fault_hook("before:pending_ready")
                preparation = self._candidate_service.prepare(document)
                self._fault_hook("after_effect:pending_ready")
                result = self._checkpoint(
                    operation_id,
                    status="snapshotting",
                    checkpoint="pending_ready",
                    candidate_token_hash=_token_hash(preparation.marker),
                )
            elif result.checkpoint == "pending_ready":
                self._ensure_snapshot(operation_id, handoff_path)
                result = self._checkpoint(
                    operation_id,
                    status="snapshotting",
                    checkpoint="snapshot_verified",
                )
            elif result.checkpoint == "snapshot_verified":
                await self._rename_old(document)
                result = self._checkpoint(
                    operation_id,
                    status="switching",
                    checkpoint="old_renamed",
                )
            elif result.checkpoint == "old_renamed":
                preparation = self._candidate_service.prepare(document)
                candidate_id = await self._ensure_candidate(
                    document,
                    result,
                    preparation,
                )
                result = self._checkpoint(
                    operation_id,
                    status="switching",
                    checkpoint="candidate_created",
                    candidate_container_id=candidate_id,
                )
            elif result.checkpoint == "candidate_created":
                preparation = self._candidate_service.prepare(document)
                candidate_id = await self._require_candidate_identity(
                    document,
                    result,
                    preparation,
                )
                await self._start_candidate(candidate_id)
                result = self._checkpoint(
                    operation_id,
                    status="switching",
                    checkpoint="candidate_started",
                )
            elif result.checkpoint == "candidate_started" and result.status == "switching":
                result = self._checkpoint(
                    operation_id,
                    status="verifying",
                    checkpoint="candidate_started",
                )
            elif result.checkpoint == "candidate_started":
                preparation = self._candidate_service.prepare(document)
                candidate_id = await self._require_candidate_identity(
                    document,
                    result,
                    preparation,
                )
                self._fault_hook("before:candidate_verified")
                await self._verifier.verify(document, preparation, candidate_id)
                self._fault_hook("after_effect:candidate_verified")
                result = self._checkpoint(
                    operation_id,
                    status="verifying",
                    checkpoint="candidate_verified",
                )
            elif result.checkpoint == "candidate_verified":
                result = self._checkpoint(
                    operation_id,
                    status="commit_requested",
                    checkpoint="commit_requested",
                )
            elif result.checkpoint == "commit_requested":
                self._fault_hook("before:commit_confirmed")
                await self._commit_waiter.wait(operation_id=operation_id)
                self._fault_hook("after_effect:commit_confirmed")
                result = self._checkpoint(
                    operation_id,
                    status="success",
                    checkpoint="commit_requested",
                )
            else:
                raise UpdaterStateMachineError("updater v2 正常路径检查点无法执行")

        candidate_id = _require_candidate_id(result)
        await self._cleanup_old(document, result)
        return candidate_id

    def _load_or_start(self, document: HandoffDocument) -> UpdaterResultV2:
        path = self._journal.directory / f"{document.operation_id}.json"
        if not path.exists() and not path.is_symlink():
            self._fault_hook("before:initialized")
            result = self._journal.start_v2(
                document=document,
                coordinator_container_id=self._coordinator_container_id,
            )
            self._fault_hook("after_checkpoint:initialized")
            return result
        result = self._journal.read(operation_id=document.operation_id)
        if not isinstance(result, UpdaterResultV2):
            raise UpdaterStateMachineError("schema v1 正常路径不支持自动恢复")
        return result

    def _validate_resumable(self, result: UpdaterResultV2) -> None:
        if result.coordinator_container_id != self._coordinator_container_id:
            raise UpdaterStateMachineError("updater v2 需要先完成协调器接管")
        if result.rollback_started or result.status in {
            "rolling_back",
            "rolled_back",
            "failed",
            "rollback_failed",
        }:
            raise UpdaterStateMachineError("updater v2 当前状态不属于正常路径")

    async def _fence_old_restart(self, document: HandoffDocument) -> None:
        self._fault_hook("before:old_restart_fenced")
        container = await self._engine.inspect_container(document.current_container_id)
        self._require_source_identity(document, container, expected_previous=False)
        host = container.get("HostConfig")
        policy = host.get("RestartPolicy") if isinstance(host, dict) else None
        if not isinstance(policy, dict) or policy.get("Name") != "no":
            await self._engine.update_restart_policy(
                document.current_container_id,
                restart_policy={"Name": "no", "MaximumRetryCount": 0},
            )
        confirmed = await self._engine.inspect_container(document.current_container_id)
        self._require_source_identity(document, confirmed, expected_previous=False)
        confirmed_host = confirmed.get("HostConfig")
        confirmed_policy = (
            confirmed_host.get("RestartPolicy")
            if isinstance(confirmed_host, dict)
            else None
        )
        if not isinstance(confirmed_policy, dict) or confirmed_policy.get("Name") != "no":
            raise UpdaterStateMachineError("旧容器重启隔离状态未确认")
        self._fault_hook("after_effect:old_restart_fenced")

    async def _stop_old(self, document: HandoffDocument) -> None:
        self._fault_hook("before:old_stopped")
        container = await self._engine.inspect_container(document.current_container_id)
        self._require_source_identity(document, container, expected_previous=False)
        state = container.get("State")
        if not isinstance(state, dict):
            raise UpdaterStateMachineError("旧容器运行状态无效")
        if state.get("Running") is True:
            await self._engine.stop_container(
                document.current_container_id,
                timeout_seconds=self._stop_timeout_seconds,
            )
            await self._engine.wait_container(document.current_container_id)
        confirmed = await self._engine.inspect_container(document.current_container_id)
        self._require_source_identity(document, confirmed, expected_previous=False)
        confirmed_state = confirmed.get("State")
        if not isinstance(confirmed_state, dict) or confirmed_state.get("Running") is True:
            raise UpdaterStateMachineError("旧容器停止状态未确认")
        self._fault_hook("after_effect:old_stopped")

    def _ensure_snapshot(self, operation_id: str, handoff_path: Path) -> None:
        self._fault_hook("before:snapshot_verified")
        directory = self._snapshot_service.backup_root / operation_id
        if not directory.exists():
            self._snapshot_service.create(
                operation_id=operation_id,
                handoff_path=handoff_path,
            )
        self._snapshot_service.verify(operation_id=operation_id)
        self._fault_hook("after_effect:snapshot_verified")

    async def _rename_old(self, document: HandoffDocument) -> None:
        self._fault_hook("before:old_renamed")
        expected_previous = previous_container_name(document.operation_id)
        container = await self._engine.inspect_container(document.current_container_id)
        name = container.get("Name")
        normalized = name.removeprefix("/") if isinstance(name, str) else ""
        if normalized == document.candidate.name:
            self._require_source_identity(document, container, expected_previous=False)
            await self._engine.rename_container(
                document.current_container_id,
                name=expected_previous,
            )
        elif normalized != expected_previous:
            raise UpdaterStateMachineError("旧容器名称与更新检查点冲突")
        else:
            self._require_source_identity(document, container, expected_previous=True)
        confirmed = await self._engine.inspect_container(document.current_container_id)
        self._require_source_identity(document, confirmed, expected_previous=True)
        self._fault_hook("after_effect:old_renamed")

    async def _ensure_candidate(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
        preparation: CandidatePreparation,
    ) -> str:
        self._fault_hook("before:candidate_created")
        observation = await UpdaterDockerIdentityService(
            engine=self._engine,
            socket_path=self._socket_path,
        ).observe(
            document=document,
            result=result,
            marker=preparation.marker,
            hostname=self._coordinator_container_id[:12],
        )
        if observation.candidate.status == "missing":
            await self._engine.create_container(
                name=document.candidate.name,
                config=preparation.create_config,
            )
            observation = await UpdaterDockerIdentityService(
                engine=self._engine,
                socket_path=self._socket_path,
            ).observe(
                document=document,
                result=result,
                marker=preparation.marker,
                hostname=self._coordinator_container_id[:12],
            )
        if (
            observation.source.status != "matched"
            or observation.coordinator.status != "matched"
            or observation.candidate.status != "matched"
            or observation.candidate.container_id is None
        ):
            raise UpdaterStateMachineError("无法唯一确认新建候选容器身份")
        self._fault_hook("after_effect:candidate_created")
        return observation.candidate.container_id

    async def _start_candidate(self, candidate_id: str) -> None:
        self._fault_hook("before:candidate_started")
        container = await self._engine.inspect_container(candidate_id)
        state = container.get("State")
        if not isinstance(state, dict):
            raise UpdaterStateMachineError("候选容器运行状态无效")
        if state.get("Running") is not True:
            await self._engine.start_container(candidate_id)
        confirmed = await self._engine.inspect_container(candidate_id)
        confirmed_state = confirmed.get("State")
        if not isinstance(confirmed_state, dict) or confirmed_state.get("Running") is not True:
            raise UpdaterStateMachineError("候选容器启动状态未确认")
        self._fault_hook("after_effect:candidate_started")

    async def _require_candidate_identity(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
        preparation: CandidatePreparation,
    ) -> str:
        observation = await UpdaterDockerIdentityService(
            engine=self._engine,
            socket_path=self._socket_path,
        ).observe(
            document=document,
            result=result,
            marker=preparation.marker,
            hostname=self._coordinator_container_id[:12],
        )
        expected_id = _require_candidate_id(result)
        if (
            observation.source.status != "matched"
            or observation.coordinator.status != "matched"
            or observation.candidate.status != "matched"
            or observation.candidate.container_id != expected_id
        ):
            raise UpdaterStateMachineError("候选容器身份与 v2 检查点冲突")
        return expected_id

    async def _cleanup_old(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
    ) -> None:
        self._fault_hook("before:old_removed")
        summaries = await self._engine.list_containers()
        if any(item.get("Id") == document.current_container_id for item in summaries):
            container = await self._engine.inspect_container(
                document.current_container_id
            )
            if not source_identity_matches(container, document, result):
                raise UpdaterStateMachineError("成功清理前旧容器身份不匹配")
            await self._engine.remove_container(document.current_container_id)
        self._fault_hook("after_effect:old_removed")

    @staticmethod
    def _require_source_identity(
        document: HandoffDocument,
        container: dict[str, Any],
        *,
        expected_previous: bool,
    ) -> None:
        name = container.get("Name")
        normalized = name.removeprefix("/") if isinstance(name, str) else ""
        expected_name = (
            previous_container_name(document.operation_id)
            if expected_previous
            else document.candidate.name
        )
        expected_mount = next(
            (mount for mount in document.candidate.mounts if mount.target == "/data"),
            None,
        )
        mounts = container.get("Mounts")
        matched_mounts = (
            [
                item
                for item in mounts
                if isinstance(item, dict) and item.get("Destination") == "/data"
            ]
            if isinstance(mounts, list)
            else []
        )
        if expected_mount is None or len(matched_mounts) != 1:
            raise UpdaterStateMachineError("旧容器身份与 handoff 不匹配")
        actual_mount = matched_mounts[0]
        actual_source = (
            actual_mount.get("Name")
            if expected_mount.type == "volume"
            else actual_mount.get("Source")
        )
        if (
            container.get("Id") != document.current_container_id
            or container.get("Image") != document.source_image_id
            or normalized != expected_name
            or actual_mount.get("Type") != expected_mount.type
            or actual_source != expected_mount.source
            or actual_mount.get("RW") is not (not expected_mount.read_only)
        ):
            raise UpdaterStateMachineError("旧容器身份与 handoff 不匹配")

    def _checkpoint(
        self,
        operation_id: str,
        *,
        status: UpdaterStatus,
        checkpoint: UpdaterCheckpoint,
        candidate_token_hash: str | None = None,
        candidate_container_id: str | None = None,
    ) -> UpdaterResultV2:
        self._fault_hook(f"before_checkpoint:{status}:{checkpoint}")
        result = self._journal.checkpoint_v2(
            operation_id=operation_id,
            status=status,
            checkpoint=checkpoint,
            candidate_token_hash=candidate_token_hash,
            candidate_container_id=candidate_container_id,
        )
        self._fault_hook(f"after_checkpoint:{status}:{checkpoint}")
        return result


def _token_hash(marker: PendingUpdateMarker) -> str:
    return "sha256:" + hashlib.sha256(marker.candidate_token.encode()).hexdigest()


def _require_candidate_id(result: UpdaterResultV2) -> str:
    if result.candidate_container_id is None:
        raise UpdaterStateMachineError("updater v2 检查点缺少候选容器标识")
    return result.candidate_container_id
