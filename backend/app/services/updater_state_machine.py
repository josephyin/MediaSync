from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.appliance.health import REQUIRED_COMPONENTS
from app.services.candidate_evidence_service import (
    CandidateEvidenceError,
    read_candidate_evidence,
)
from app.services.docker_capability_service import DockerEngineError
from app.services.image_target_service import (
    DIGEST_PATTERN,
    OFFICIAL_REGISTRIES,
    ImageTargetError,
    VerifiedImageTarget,
    validate_pulled_image,
)
from app.services.update_execution_gate import PendingUpdateMarker, UpdateExecutionGate
from app.services.update_snapshot_service import (
    HandoffDocument,
    UpdaterResultJournal,
    UpdateSnapshotError,
    UpdateSnapshotService,
    fsync_directory,
    read_handoff,
)
from app.services.updater_candidate_service import (
    CandidatePreparation,
    UpdaterCandidateError,
    UpdaterCandidateService,
)
from app.services.updater_handoff_service import (
    UpdaterHandoffError,
    validate_operation_id,
)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class UpdaterStateMachineError(RuntimeError):
    pass


class UpdaterEngine(Protocol):
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

    async def inspect_container(self, container_id: str) -> dict[str, Any]: ...

    async def inspect_image(self, reference: str) -> dict[str, Any] | None: ...

    async def remove_container(self, container_id: str) -> None: ...


class CandidateVerifier(Protocol):
    async def verify(
        self,
        document: HandoffDocument,
        preparation: CandidatePreparation,
        candidate_id: str,
    ) -> None: ...


class PreviousContainerVerifier(Protocol):
    async def verify(
        self,
        document: HandoffDocument,
        container_id: str,
    ) -> None: ...


class CommitWaiter(Protocol):
    async def wait(self, *, operation_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class CandidateObservation:
    fingerprint: tuple[str, int, str, datetime]


@dataclass(frozen=True, slots=True)
class PreviousContainerObservation:
    fingerprint: tuple[str, str, int, str]


class CandidateHealthVerifier:
    def __init__(
        self,
        *,
        engine: UpdaterEngine,
        data_directory: Path,
        pending_path: Path,
        stable_seconds: float = 30,
        timeout_seconds: float = 180,
        poll_seconds: float = 2,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if stable_seconds <= 0 or timeout_seconds < stable_seconds or poll_seconds <= 0:
            raise ValueError("候选验证时间参数无效")
        self._engine = engine
        self._data_directory = Path(data_directory)
        self._pending_path = Path(pending_path)
        self._stable_seconds = stable_seconds
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep

    async def verify(
        self,
        document: HandoffDocument,
        preparation: CandidatePreparation,
        candidate_id: str,
    ) -> None:
        deadline = self._clock() + self._timeout_seconds
        stable_since: float | None = None
        fingerprint: tuple[str, int, str, datetime] | None = None
        while self._clock() <= deadline:
            observation = await self._observe(
                document,
                preparation.marker,
                candidate_id,
            )
            if observation is None:
                stable_since = None
                fingerprint = None
            elif stable_since is None:
                stable_since = self._clock()
                fingerprint = observation.fingerprint
            elif observation.fingerprint != fingerprint:
                raise UpdaterStateMachineError("候选容器在稳定观察期间发生重启或替换")
            elif self._clock() - stable_since >= self._stable_seconds:
                return
            await self._sleep(self._poll_seconds)
        raise UpdaterStateMachineError("候选容器健康验证超时")

    async def _observe(
        self,
        document: HandoffDocument,
        marker: PendingUpdateMarker,
        candidate_id: str,
    ) -> CandidateObservation | None:
        current_marker = UpdateExecutionGate(
            pending_path=str(self._pending_path)
        ).read_pending_marker()
        if current_marker != marker:
            raise UpdaterStateMachineError("候选验证标记在观察期间发生变化")

        container = await self._engine.inspect_container(candidate_id)
        if container.get("Id") != candidate_id:
            raise UpdaterStateMachineError("Docker 返回的候选容器身份不匹配")
        config = container.get("Config")
        if not isinstance(config, dict) or config.get("Image") != document.target_image:
            raise UpdaterStateMachineError("候选容器镜像引用不匹配")
        image_id = container.get("Image")
        if not isinstance(image_id, str) or not DIGEST_PATTERN.fullmatch(image_id):
            raise UpdaterStateMachineError("候选容器镜像标识无效")
        image = await self._engine.inspect_image(document.target_image)
        if image is None or image.get("Id") != image_id:
            raise UpdaterStateMachineError("候选容器镜像身份不匹配")
        target = target_from_handoff(document)
        try:
            revision = validate_pulled_image(image, target)
        except ImageTargetError as exc:
            raise UpdaterStateMachineError(str(exc)) from exc
        if revision != document.target_revision:
            raise UpdaterStateMachineError("候选容器源码修订不匹配")

        state = container.get("State")
        if not isinstance(state, dict):
            raise UpdaterStateMachineError("候选容器运行状态无效")
        health = state.get("Health")
        health_status = health.get("Status") if isinstance(health, dict) else None
        if state.get("Running") is not True:
            raise UpdaterStateMachineError("候选容器已退出")
        if health_status == "unhealthy":
            raise UpdaterStateMachineError("候选容器健康检查失败")
        if health_status != "healthy":
            return None
        restart_count = container.get("RestartCount")
        started_at = state.get("StartedAt")
        if (
            not isinstance(restart_count, int)
            or isinstance(restart_count, bool)
            or restart_count < 0
            or not isinstance(started_at, str)
            or not started_at
        ):
            raise UpdaterStateMachineError("候选容器稳定性字段无效")
        started_at_datetime = parse_docker_datetime(started_at)

        evidence_path = (
            self._data_directory
            / "update"
            / "operations"
            / f"{document.operation_id}.candidate.json"
        )
        if evidence_path.is_symlink():
            raise UpdaterStateMachineError("候选验证证据路径不安全")
        if not evidence_path.exists():
            return None
        try:
            evidence = read_candidate_evidence(
                evidence_path,
                expected_operation_id=document.operation_id,
                expected_candidate_token=marker.candidate_token,
            )
        except CandidateEvidenceError as exc:
            raise UpdaterStateMachineError(str(exc)) from exc
        if (
            evidence.version != document.target_version.removeprefix("v")
            or evidence.revision != document.target_revision
            or evidence.digest != document.target_digest
            or evidence.observed_at < started_at_datetime
        ):
            raise UpdaterStateMachineError("候选验证证据与当前容器不匹配")
        return CandidateObservation(
            fingerprint=(
                candidate_id,
                restart_count,
                started_at,
                evidence.observed_at,
            ),
        )


class PreviousContainerHealthVerifier:
    def __init__(
        self,
        *,
        engine: UpdaterEngine,
        stable_seconds: float = 30,
        timeout_seconds: float = 180,
        poll_seconds: float = 2,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if stable_seconds <= 0 or timeout_seconds < stable_seconds or poll_seconds <= 0:
            raise ValueError("旧容器验证时间参数无效")
        self._engine = engine
        self._stable_seconds = stable_seconds
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep

    async def verify(
        self,
        document: HandoffDocument,
        container_id: str,
    ) -> None:
        if container_id != document.current_container_id:
            raise UpdaterStateMachineError("旧容器标识与 handoff 不匹配")
        deadline = self._clock() + self._timeout_seconds
        stable_since: float | None = None
        fingerprint: tuple[str, str, int, str] | None = None
        while self._clock() <= deadline:
            observation = await self._observe(document, container_id)
            if observation is None:
                stable_since = None
                fingerprint = None
            elif stable_since is None:
                stable_since = self._clock()
                fingerprint = observation.fingerprint
            elif observation.fingerprint != fingerprint:
                raise UpdaterStateMachineError("旧容器在稳定观察期间发生重启或替换")
            elif self._clock() - stable_since >= self._stable_seconds:
                return
            await self._sleep(self._poll_seconds)
        raise UpdaterStateMachineError("旧容器健康验证超时")

    async def _observe(
        self,
        document: HandoffDocument,
        container_id: str,
    ) -> PreviousContainerObservation | None:
        container = await self._engine.inspect_container(container_id)
        name = container.get("Name")
        if (
            container.get("Id") != container_id
            or container.get("Image") != document.source_image_id
            or not isinstance(name, str)
            or name.removeprefix("/") != document.candidate.name
        ):
            raise UpdaterStateMachineError("Docker 返回的旧容器身份不匹配")
        state = container.get("State")
        if not isinstance(state, dict):
            raise UpdaterStateMachineError("旧容器运行状态无效")
        if state.get("Running") is not True:
            return None
        health = state.get("Health")
        if not isinstance(health, dict):
            raise UpdaterStateMachineError("旧容器健康状态无效")
        health_status = health.get("Status")
        if health_status == "unhealthy":
            raise UpdaterStateMachineError("旧容器健康检查失败")
        if health_status != "healthy":
            return None
        restart_count = container.get("RestartCount")
        started_at = state.get("StartedAt")
        if (
            not isinstance(restart_count, int)
            or isinstance(restart_count, bool)
            or restart_count < 0
            or not isinstance(started_at, str)
            or not started_at
        ):
            raise UpdaterStateMachineError("旧容器稳定性字段无效")
        parse_docker_datetime(started_at)
        self._validate_health_output(health.get("Log"))
        return PreviousContainerObservation(
            fingerprint=(container_id, document.source_image_id, restart_count, started_at)
        )

    @staticmethod
    def _validate_health_output(value: object) -> None:
        if not isinstance(value, list) or not value:
            raise UpdaterStateMachineError("旧容器缺少五组件健康证据")
        latest = value[-1]
        output = latest.get("Output") if isinstance(latest, dict) else None
        if not isinstance(output, str):
            raise UpdaterStateMachineError("旧容器五组件健康证据无效")
        try:
            if len(output.encode("utf-8")) > 16 * 1024:
                raise UpdaterStateMachineError("旧容器五组件健康证据无效")
            payload = json.loads(output.strip())
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise UpdaterStateMachineError("旧容器五组件健康证据无效") from exc
        if not isinstance(payload, dict) or any(
            payload.get(component) is not True for component in REQUIRED_COMPONENTS
        ):
            raise UpdaterStateMachineError("旧容器五组件未全部健康")


class ApplianceCommitWaiter:
    def __init__(
        self,
        *,
        data_directory: Path,
        pending_path: Path,
        timeout_seconds: float,
        poll_seconds: float,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("终态等待时间参数无效")
        self._data_directory = Path(data_directory)
        self._database_path = self._data_directory / "mediasync.db"
        self._pending_path = Path(pending_path)
        self._timeout_seconds = timeout_seconds
        self._poll_seconds = poll_seconds
        self._clock = clock
        self._sleep = sleep

    async def wait(self, *, operation_id: str) -> None:
        validate_operation_id(operation_id)
        deadline = self._clock() + self._timeout_seconds
        while self._clock() <= deadline:
            if self._runtime_markers_removed(operation_id) and self._database_committed(
                operation_id
            ):
                return
            await self._sleep(self._poll_seconds)
        raise UpdaterStateMachineError("等待 Appliance 提交更新终态超时")

    def _database_committed(self, operation_id: str) -> bool:
        try:
            connection = sqlite3.connect(
                f"file:{self._database_path}?mode=ro",
                uri=True,
                timeout=1,
            )
            try:
                row = connection.execute(
                    "SELECT status, active_slot FROM update_operations "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            return False
        return row == ("success", None)

    def _runtime_markers_removed(self, operation_id: str) -> bool:
        operations = self._data_directory / "update" / "operations"
        paths = (
            self._pending_path,
            operations / f"{operation_id}.candidate.json",
            operations / f"{operation_id}.handoff.json",
        )
        return not any(path.exists() or path.is_symlink() for path in paths)


class UpdaterStateMachine:
    def __init__(
        self,
        *,
        engine: UpdaterEngine,
        data_directory: Path,
        snapshot_service: UpdateSnapshotService,
        candidate_service: UpdaterCandidateService,
        journal: UpdaterResultJournal,
        verifier: CandidateVerifier,
        previous_verifier: PreviousContainerVerifier,
        commit_waiter: CommitWaiter,
        stop_timeout_seconds: int = 90,
    ) -> None:
        if not 1 <= stop_timeout_seconds <= 600:
            raise ValueError("旧容器停止超时时间无效")
        self._engine = engine
        self._data_directory = Path(data_directory)
        self._snapshot_service = snapshot_service
        self._candidate_service = candidate_service
        self._journal = journal
        self._verifier = verifier
        self._previous_verifier = previous_verifier
        self._commit_waiter = commit_waiter
        self._stop_timeout_seconds = stop_timeout_seconds

    async def execute(self, *, operation_id: str) -> str:
        validate_operation_id(operation_id)
        handoff_path = (
            self._data_directory
            / "update"
            / "operations"
            / f"{operation_id}.handoff.json"
        )
        phase = "读取 handoff"
        document: HandoffDocument | None = None
        old_stopped = False
        snapshot_verified = False
        pending_ready = False
        old_renamed = False
        candidate_id: str | None = None
        try:
            document = read_handoff(
                handoff_path,
                expected_operation_id=operation_id,
            )
            phase = "初始化结果日志"
            self._journal.start(operation_id=operation_id)
            phase = "隔离旧容器重启策略"
            await self._engine.update_restart_policy(
                document.current_container_id,
                restart_policy={"Name": "no", "MaximumRetryCount": 0},
            )
            phase = "停止旧容器"
            await self._engine.stop_container(
                document.current_container_id,
                timeout_seconds=self._stop_timeout_seconds,
            )
            await self._engine.wait_container(document.current_container_id)
            old_stopped = True
            phase = "准备候选配置"
            preparation = self._candidate_service.prepare(document)
            pending_ready = True
            phase = "创建数据快照"
            self._snapshot_service.create(
                operation_id=operation_id,
                handoff_path=handoff_path,
            )
            self._snapshot_service.verify(operation_id=operation_id)
            snapshot_verified = True
            self._journal.transition(operation_id=operation_id, status="switching")
            phase = "切换容器"
            await self._engine.rename_container(
                document.current_container_id,
                name=previous_container_name(operation_id),
            )
            old_renamed = True
            candidate_id = await self._engine.create_container(
                name=document.candidate.name,
                config=preparation.create_config,
            )
            await self._engine.start_container(candidate_id)
            self._journal.transition(operation_id=operation_id, status="verifying")
            phase = "验证候选容器"
            await self._verifier.verify(document, preparation, candidate_id)
            self._journal.transition(
                operation_id=operation_id,
                status="commit_requested",
            )
            phase = "等待提交确认"
            await self._commit_waiter.wait(operation_id=operation_id)
            self._journal.transition(operation_id=operation_id, status="success")
            phase = "清理旧容器"
            await self._engine.remove_container(document.current_container_id)
            return candidate_id
        except (
            DockerEngineError,
            UpdateSnapshotError,
            UpdaterCandidateError,
            UpdaterHandoffError,
            OSError,
            UpdaterStateMachineError,
            ValueError,
        ) as exc:
            if document is not None and old_stopped and not self._commit_started(operation_id):
                try:
                    await self._rollback(
                        document=document,
                        operation_id=operation_id,
                        candidate_id=candidate_id,
                        snapshot_verified=snapshot_verified,
                        pending_ready=pending_ready,
                        old_renamed=old_renamed,
                    )
                except (
                    DockerEngineError,
                    UpdateSnapshotError,
                    UpdaterStateMachineError,
                    OSError,
                    ValueError,
                ) as rollback_exc:
                    self._record_rollback_failed(operation_id)
                    raise UpdaterStateMachineError(
                        f"updater 自动回滚失败：{phase}"
                    ) from rollback_exc
                raise UpdaterStateMachineError(
                    f"updater 正常切换失败，已自动回滚：{phase}"
                ) from exc
            raise UpdaterStateMachineError(
                f"updater 正常切换阶段失败：{phase}"
            ) from exc

    def _commit_started(self, operation_id: str) -> bool:
        try:
            result = self._journal.read(operation_id=operation_id)
        except UpdateSnapshotError:
            return False
        return result.status in {"commit_requested", "success"}

    async def _rollback(
        self,
        *,
        document: HandoffDocument,
        operation_id: str,
        candidate_id: str | None,
        snapshot_verified: bool,
        pending_ready: bool,
        old_renamed: bool,
    ) -> None:
        self._journal.transition(operation_id=operation_id, status="rolling_back")
        if candidate_id is not None:
            await self._engine.stop_container(
                candidate_id,
                timeout_seconds=self._stop_timeout_seconds,
            )
            await self._engine.wait_container(candidate_id)
            await self._engine.remove_container(candidate_id)
        if snapshot_verified:
            self._snapshot_service.restore(operation_id=operation_id)
        self._remove_candidate_evidence(operation_id)
        if old_renamed:
            await self._engine.rename_container(
                document.current_container_id,
                name=document.candidate.name,
            )
        await self._engine.update_restart_policy(
            document.current_container_id,
            restart_policy=document.candidate.restart_policy,
        )
        await self._engine.start_container(document.current_container_id)
        await self._previous_verifier.verify(
            document,
            document.current_container_id,
        )
        if not pending_ready:
            raise UpdaterStateMachineError("回滚缺少可供旧 Appliance 对账的 pending 标记")
        self._journal.transition(operation_id=operation_id, status="rolled_back")

    def _remove_candidate_evidence(self, operation_id: str) -> None:
        operations_directory = self._data_directory / "update" / "operations"
        if operations_directory.is_symlink() or not operations_directory.is_dir():
            raise UpdaterStateMachineError("更新运行目录不安全")
        evidence_path = operations_directory / f"{operation_id}.candidate.json"
        evidence_path.unlink(missing_ok=True)
        if operations_directory.exists():
            fsync_directory(operations_directory)

    def _record_rollback_failed(self, operation_id: str) -> None:
        try:
            self._journal.transition(
                operation_id=operation_id,
                status="rollback_failed",
                error_code="automatic_rollback_failed",
                public_error_message="自动回滚未能安全完成，需要人工恢复",
            )
        except UpdateSnapshotError:
            return


def previous_container_name(operation_id: str) -> str:
    validate_operation_id(operation_id)
    return f"mediasync-previous-{operation_id.split('-', 1)[0]}"


def target_from_handoff(document: HandoffDocument) -> VerifiedImageTarget:
    for key, registry in OFFICIAL_REGISTRIES.items():
        if document.target_image == f"{registry.repository}@{document.target_digest}":
            return VerifiedImageTarget(
                registry=key,
                repository=registry.repository,
                version=document.target_version,
                digest=document.target_digest,
                revision=document.target_revision,
            )
    raise UpdaterStateMachineError("handoff 目标镜像不属于官方仓库")


def parse_docker_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdaterStateMachineError("候选容器启动时间无效") from exc
    if parsed.tzinfo is None:
        raise UpdaterStateMachineError("候选容器启动时间缺少时区")
    return parsed.astimezone(UTC)
