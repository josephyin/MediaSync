from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from app.services.update_snapshot_service import (
    HandoffDocument,
    UpdaterCheckpoint,
    UpdaterResultJournal,
    UpdaterResultV2,
    UpdaterStatus,
    UpdateSnapshotService,
    fsync_directory,
    read_handoff,
)
from app.services.updater_candidate_service import (
    CandidatePreparation,
    UpdaterCandidateService,
)
from app.services.updater_recovery_decision_service import (
    DockerRecoveryObservation,
    UpdaterDockerIdentityService,
    source_identity_matches,
)
from app.services.updater_state_machine import (
    PreviousContainerVerifier,
    UpdaterStateMachineError,
    previous_container_name,
)

FaultHook = Callable[[str], None]
CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UpdaterRollbackEngine(Protocol):
    async def list_containers(self) -> list[dict[str, Any]]: ...

    async def inspect_container(self, container_id: str) -> dict[str, Any]: ...

    async def stop_container(self, container_id: str, *, timeout_seconds: int) -> None: ...

    async def wait_container(self, container_id: str) -> int: ...

    async def remove_container(self, container_id: str) -> None: ...

    async def rename_container(self, container_id: str, *, name: str) -> None: ...

    async def update_restart_policy(
        self,
        container_id: str,
        *,
        restart_policy: dict[str, Any],
    ) -> None: ...

    async def start_container(self, container_id: str) -> None: ...


class UpdaterRollbackV2:
    """按 v2 回滚检查点恢复旧 Appliance，且不创建新候选。"""

    def __init__(
        self,
        *,
        engine: UpdaterRollbackEngine,
        data_directory: Path,
        socket_path: str,
        coordinator_container_id: str,
        snapshot_service: UpdateSnapshotService,
        candidate_service: UpdaterCandidateService,
        journal: UpdaterResultJournal,
        previous_verifier: PreviousContainerVerifier,
        stop_timeout_seconds: int = 90,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if not CONTAINER_ID_PATTERN.fullmatch(coordinator_container_id):
            raise ValueError("updater 协调器必须使用完整容器标识")
        if not 1 <= stop_timeout_seconds <= 600:
            raise ValueError("候选容器停止超时时间无效")
        self._engine = engine
        self._data_directory = Path(data_directory)
        self._socket_path = socket_path
        self._coordinator_container_id = coordinator_container_id
        self._snapshot_service = snapshot_service
        self._candidate_service = candidate_service
        self._journal = journal
        self._previous_verifier = previous_verifier
        self._stop_timeout_seconds = stop_timeout_seconds
        self._fault_hook = fault_hook or (lambda _event: None)

    async def execute(self, *, operation_id: str) -> None:
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
        result = self._read_result(operation_id)
        if result.status == "rolled_back":
            return
        try:
            result, preparation = await self._enter_or_resume(
                document,
                result,
            )
            while result.status != "rolled_back":
                if result.checkpoint == "rollback_started":
                    await self._stop_candidate(document, result, preparation)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="candidate_stopped",
                    )
                elif result.checkpoint == "candidate_stopped":
                    await self._remove_candidate(document, result, preparation)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="candidate_removed",
                    )
                elif result.checkpoint == "candidate_removed":
                    self._restore_snapshot(operation_id)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="snapshot_restored",
                    )
                elif result.checkpoint == "snapshot_restored":
                    self._remove_candidate_evidence(operation_id)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="candidate_evidence_removed",
                    )
                elif result.checkpoint == "candidate_evidence_removed":
                    await self._restore_old_name(document, result)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="old_name_restored",
                    )
                elif result.checkpoint == "old_name_restored":
                    await self._restore_old_policy(document, result)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="old_policy_restored",
                    )
                elif result.checkpoint == "old_policy_restored":
                    await self._start_old(document, result)
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="old_started",
                    )
                elif result.checkpoint == "old_started":
                    self._fault_hook("before:old_verified")
                    await self._previous_verifier.verify(
                        document,
                        document.current_container_id,
                    )
                    self._fault_hook("after_effect:old_verified")
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="old_verified",
                    )
                elif result.checkpoint == "old_verified":
                    self._fault_hook("before:rollback_published")
                    self._fault_hook("after_effect:rollback_published")
                    result = self._checkpoint(
                        operation_id,
                        status="rolling_back",
                        checkpoint="rollback_published",
                    )
                elif result.checkpoint == "rollback_published":
                    result = self._checkpoint(
                        operation_id,
                        status="rolled_back",
                        checkpoint="rollback_published",
                    )
                else:
                    raise UpdaterStateMachineError("updater v2 回滚检查点无法执行")
        except UpdaterStateMachineError:
            self._record_rollback_failed(operation_id)
            raise
        except Exception as exc:
            raise UpdaterStateMachineError(
                "updater v2 自动回滚暂时失败，可从当前检查点重试"
            ) from exc

    def _read_result(self, operation_id: str) -> UpdaterResultV2:
        result = self._journal.read(operation_id=operation_id)
        if not isinstance(result, UpdaterResultV2):
            raise UpdaterStateMachineError("schema v1 不支持自动回滚")
        if result.coordinator_container_id != self._coordinator_container_id:
            raise UpdaterStateMachineError("updater v2 需要先完成协调器接管")
        if result.status in {"commit_requested", "success"}:
            raise UpdaterStateMachineError("提交阶段禁止自动回滚")
        if result.status in {"failed", "rollback_failed"}:
            raise UpdaterStateMachineError("updater v2 当前状态不可再次回滚")
        return result

    async def _enter_or_resume(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
    ) -> tuple[UpdaterResultV2, CandidatePreparation]:
        if result.status != "rolling_back":
            self._fault_hook("before:rollback_started")
        preparation = self._candidate_service.prepare(document)
        expected_hash = _token_hash(preparation)
        if result.candidate_token_hash not in {None, expected_hash}:
            raise UpdaterStateMachineError("回滚 pending 与结果身份不匹配")
        if result.status == "rolling_back":
            return result, preparation
        if result.status not in {"snapshotting", "switching", "verifying"}:
            raise UpdaterStateMachineError("当前状态不允许开始自动回滚")

        observation = await self._observe(document, result, preparation)
        if (
            observation.source.status != "matched"
            or observation.source.running is not False
            or observation.coordinator.status != "matched"
            or (
                observation.candidate.status == "conflict"
                and result.candidate_container_id is None
            )
        ):
            raise UpdaterStateMachineError("回滚起点容器身份无法安全确认")
        candidate_id = (
            result.candidate_container_id or observation.candidate.container_id
        )
        self._fault_hook("after_effect:rollback_started")
        result = self._checkpoint(
            document.operation_id,
            status="rolling_back",
            checkpoint="rollback_started",
            candidate_token_hash=expected_hash,
            candidate_container_id=candidate_id,
            rollback_started=True,
        )
        return result, preparation

    async def _stop_candidate(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
        preparation: CandidatePreparation,
    ) -> None:
        self._fault_hook("before:candidate_stopped")
        observation = await self._observe(document, result, preparation)
        self._require_safe_observation(observation)
        if observation.candidate.status == "matched" and observation.candidate.running:
            candidate_id = _require_candidate_id(observation)
            await self._engine.stop_container(
                candidate_id,
                timeout_seconds=self._stop_timeout_seconds,
            )
            await self._engine.wait_container(candidate_id)
        confirmed = await self._observe(document, result, preparation)
        self._require_safe_observation(confirmed)
        if confirmed.candidate.status == "matched" and confirmed.candidate.running:
            raise UpdaterStateMachineError("候选容器停止状态未确认")
        self._fault_hook("after_effect:candidate_stopped")

    async def _remove_candidate(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
        preparation: CandidatePreparation,
    ) -> None:
        self._fault_hook("before:candidate_removed")
        observation = await self._observe(document, result, preparation)
        self._require_safe_observation(observation)
        if observation.candidate.status == "matched":
            if observation.candidate.running:
                raise UpdaterStateMachineError("运行中的候选容器不得删除")
            await self._engine.remove_container(_require_candidate_id(observation))
        confirmed = await self._observe(document, result, preparation)
        self._require_safe_observation(confirmed)
        if confirmed.candidate.status != "missing":
            raise UpdaterStateMachineError("候选容器删除状态未确认")
        self._fault_hook("after_effect:candidate_removed")

    def _restore_snapshot(self, operation_id: str) -> None:
        self._fault_hook("before:snapshot_restored")
        directory = self._snapshot_service.backup_root / operation_id
        if directory.exists() or directory.is_symlink():
            self._snapshot_service.verify(operation_id=operation_id)
            self._snapshot_service.restore(operation_id=operation_id)
            self._snapshot_service.verify(operation_id=operation_id)
        self._fault_hook("after_effect:snapshot_restored")

    def _remove_candidate_evidence(self, operation_id: str) -> None:
        self._fault_hook("before:candidate_evidence_removed")
        directory = self._data_directory / "update" / "operations"
        if directory.is_symlink() or not directory.is_dir():
            raise UpdaterStateMachineError("更新运行目录不安全")
        path = directory / f"{operation_id}.candidate.json"
        if path.is_symlink():
            raise UpdaterStateMachineError("候选证据路径不安全")
        path.unlink(missing_ok=True)
        fsync_directory(directory)
        self._fault_hook("after_effect:candidate_evidence_removed")

    async def _restore_old_name(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
    ) -> None:
        self._fault_hook("before:old_name_restored")
        container = await self._engine.inspect_container(document.current_container_id)
        if not source_identity_matches(container, document, result):
            raise UpdaterStateMachineError("旧容器身份与回滚结果不匹配")
        name = _container_name(container)
        previous = previous_container_name(document.operation_id)
        if name == previous:
            await self._engine.rename_container(
                document.current_container_id,
                name=document.candidate.name,
            )
        elif name != document.candidate.name:
            raise UpdaterStateMachineError("旧容器名称无法安全恢复")
        confirmed = await self._engine.inspect_container(document.current_container_id)
        if _container_name(confirmed) != document.candidate.name:
            raise UpdaterStateMachineError("旧容器名称恢复状态未确认")
        self._fault_hook("after_effect:old_name_restored")

    async def _restore_old_policy(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
    ) -> None:
        self._fault_hook("before:old_policy_restored")
        container = await self._engine.inspect_container(document.current_container_id)
        if not source_identity_matches(container, document, result):
            raise UpdaterStateMachineError("旧容器身份与回滚结果不匹配")
        current = _restart_policy(container)
        if current != document.candidate.restart_policy:
            await self._engine.update_restart_policy(
                document.current_container_id,
                restart_policy=document.candidate.restart_policy,
            )
        confirmed = await self._engine.inspect_container(document.current_container_id)
        if _restart_policy(confirmed) != document.candidate.restart_policy:
            raise UpdaterStateMachineError("旧容器重启策略恢复状态未确认")
        self._fault_hook("after_effect:old_policy_restored")

    async def _start_old(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
    ) -> None:
        self._fault_hook("before:old_started")
        container = await self._engine.inspect_container(document.current_container_id)
        if not source_identity_matches(container, document, result):
            raise UpdaterStateMachineError("旧容器身份与回滚结果不匹配")
        state = container.get("State")
        if not isinstance(state, dict):
            raise UpdaterStateMachineError("旧容器运行状态无效")
        if state.get("Running") is not True:
            await self._engine.start_container(document.current_container_id)
        confirmed = await self._engine.inspect_container(document.current_container_id)
        confirmed_state = confirmed.get("State")
        if not isinstance(confirmed_state, dict) or confirmed_state.get("Running") is not True:
            raise UpdaterStateMachineError("旧容器启动状态未确认")
        self._fault_hook("after_effect:old_started")

    async def _observe(
        self,
        document: HandoffDocument,
        result: UpdaterResultV2,
        preparation: CandidatePreparation,
    ) -> DockerRecoveryObservation:
        return await UpdaterDockerIdentityService(
            engine=self._engine,
            socket_path=self._socket_path,
        ).observe(
            document=document,
            result=result,
            marker=preparation.marker,
            hostname=self._coordinator_container_id[:12],
        )

    @staticmethod
    def _require_safe_observation(observation: DockerRecoveryObservation) -> None:
        if (
            observation.source.status != "matched"
            or observation.coordinator.status != "matched"
            or observation.candidate.status == "conflict"
        ):
            raise UpdaterStateMachineError("回滚容器身份无法安全确认")

    def _checkpoint(
        self,
        operation_id: str,
        *,
        status: UpdaterStatus,
        checkpoint: UpdaterCheckpoint,
        candidate_token_hash: str | None = None,
        candidate_container_id: str | None = None,
        rollback_started: bool | None = None,
    ) -> UpdaterResultV2:
        self._fault_hook(f"before_checkpoint:{status}:{checkpoint}")
        result = self._journal.checkpoint_v2(
            operation_id=operation_id,
            status=status,
            checkpoint=checkpoint,
            candidate_token_hash=candidate_token_hash,
            candidate_container_id=candidate_container_id,
            rollback_started=rollback_started,
        )
        self._fault_hook(f"after_checkpoint:{status}:{checkpoint}")
        return result

    def _record_rollback_failed(self, operation_id: str) -> None:
        try:
            current = self._journal.read(operation_id=operation_id)
            if not isinstance(current, UpdaterResultV2) or current.status != "rolling_back":
                return
            self._journal.checkpoint_v2(
                operation_id=operation_id,
                status="rollback_failed",
                checkpoint=current.checkpoint,
                error_code="automatic_rollback_failed",
                public_error_message="自动回滚未能安全完成，需要人工恢复",
            )
        except Exception:
            return


def _token_hash(preparation: CandidatePreparation) -> str:
    token = preparation.marker.candidate_token
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


def _require_candidate_id(observation: DockerRecoveryObservation) -> str:
    container_id = observation.candidate.container_id
    if container_id is None:
        raise UpdaterStateMachineError("回滚检查点缺少候选容器标识")
    return container_id


def _container_name(container: dict[str, Any]) -> str:
    value = container.get("Name")
    return value.removeprefix("/") if isinstance(value, str) else ""


def _restart_policy(container: dict[str, Any]) -> object:
    host = container.get("HostConfig")
    return host.get("RestartPolicy") if isinstance(host, dict) else None
