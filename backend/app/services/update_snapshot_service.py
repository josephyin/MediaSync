from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.services.image_target_service import (
    DIGEST_PATTERN,
    OFFICIAL_REGISTRIES,
    REVISION_PATTERN,
    VERSION_PATTERN,
)
from app.services.updater_handoff_service import (
    CONTAINER_ID_PATTERN,
    CONTAINER_NAME_PATTERN,
    UpdaterHandoffError,
    validate_operation_id,
)

SNAPSHOT_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
FILE_CHUNK_SIZE = 1024 * 1024
MAX_UPDATER_RESULT_BYTES = 64 * 1024
MAX_RECOVERY_GENERATION = 3
UPDATER_TERMINAL_STATUSES = frozenset(
    {"success", "failed", "rolled_back", "rollback_failed"}
)
UPDATER_TRANSITIONS = {
    "snapshotting": {"switching", "rolling_back", "failed"},
    "switching": {"verifying", "rolling_back"},
    "verifying": {"commit_requested", "rolling_back"},
    "commit_requested": {"success"},
    "rolling_back": {"rolled_back", "rollback_failed"},
}
FORWARD_CHECKPOINTS = (
    "initialized",
    "old_restart_fenced",
    "old_stopped",
    "pending_ready",
    "snapshot_verified",
    "old_renamed",
    "candidate_created",
    "candidate_started",
    "candidate_verified",
    "commit_requested",
)
ROLLBACK_CHECKPOINTS = (
    "rollback_started",
    "candidate_stopped",
    "candidate_removed",
    "snapshot_restored",
    "candidate_evidence_removed",
    "old_name_restored",
    "old_policy_restored",
    "old_started",
    "old_verified",
    "rollback_published",
)
UpdaterStatus = Literal[
    "snapshotting",
    "switching",
    "verifying",
    "commit_requested",
    "rolling_back",
    "success",
    "failed",
    "rolled_back",
    "rollback_failed",
]
UpdaterCheckpoint = Literal[
    "initialized",
    "old_restart_fenced",
    "old_stopped",
    "pending_ready",
    "snapshot_verified",
    "old_renamed",
    "candidate_created",
    "candidate_started",
    "candidate_verified",
    "commit_requested",
    "rollback_started",
    "candidate_stopped",
    "candidate_removed",
    "snapshot_restored",
    "candidate_evidence_removed",
    "old_name_restored",
    "old_policy_restored",
    "old_started",
    "old_verified",
    "rollback_published",
]


class UpdateSnapshotError(RuntimeError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HandoffMount(StrictModel):
    type: Literal["bind", "volume"]
    source: str
    target: str
    read_only: bool


class HandoffCandidate(StrictModel):
    container_id: str
    name: str
    env: tuple[str, ...]
    user: str
    labels: dict[str, str]
    mounts: tuple[HandoffMount, ...]
    exposed_ports: tuple[str, ...]
    port_bindings: dict[str, list[dict[str, str]]]
    restart_policy: dict[str, Any]
    network_mode: str
    networks: dict[str, tuple[str, ...]]
    dns: tuple[str, ...]
    group_add: tuple[str, ...]
    readonly_rootfs: bool


class HandoffDocument(StrictModel):
    schema_version: Literal[2]
    operation_id: str
    current_container_id: str
    source_image_id: str
    source_image_reference: str
    source_version: str
    source_digest: str | None
    target_version: str
    target_digest: str
    target_revision: str
    target_image: str
    candidate: HandoffCandidate


class SnapshotFile(StrictModel):
    source_path: str
    snapshot_name: str
    size: int
    sha256: str


class SnapshotManifest(StrictModel):
    schema_version: Literal[1]
    operation_id: str
    created_at: datetime
    alembic_revision: str
    source_image_id: str
    source_image_reference: str
    source_version: str
    source_digest: str | None
    files: tuple[SnapshotFile, ...]


class UpdaterResult(StrictModel):
    schema_version: Literal[1]
    operation_id: str
    sequence: int
    status: Literal[
        "snapshotting",
        "switching",
        "verifying",
        "commit_requested",
        "rolling_back",
        "success",
        "failed",
        "rolled_back",
        "rollback_failed",
    ]
    updated_at: datetime
    error_code: str | None = None
    public_error_message: str | None = None


class UpdaterResultV2(StrictModel):
    schema_version: Literal[2]
    operation_id: str
    sequence: int = Field(ge=1)
    status: UpdaterStatus
    checkpoint: UpdaterCheckpoint
    recovery_generation: int = Field(ge=0, le=MAX_RECOVERY_GENERATION)
    coordinator_container_id: str
    source_container_id: str
    source_image_id: str
    source_container_name: str
    target_image: str
    target_revision: str
    candidate_token_hash: str | None = None
    candidate_container_id: str | None = None
    rollback_started: bool
    updated_at: datetime
    error_code: str | None = None
    public_error_message: str | None = None

    @model_validator(mode="after")
    def validate_invariants(self) -> UpdaterResultV2:
        try:
            validate_operation_id(self.operation_id)
        except UpdaterHandoffError as exc:
            raise ValueError("updater v2 操作标识无效") from exc
        for value in (
            self.coordinator_container_id,
            self.source_container_id,
        ):
            if len(value) != 64 or not CONTAINER_ID_PATTERN.fullmatch(value):
                raise ValueError("updater v2 容器标识无效")
        if self.candidate_container_id is not None and (
            len(self.candidate_container_id) != 64
            or not CONTAINER_ID_PATTERN.fullmatch(self.candidate_container_id)
        ):
            raise ValueError("updater v2 候选容器标识无效")
        if not DIGEST_PATTERN.fullmatch(self.source_image_id):
            raise ValueError("updater v2 源镜像标识无效")
        if not CONTAINER_NAME_PATTERN.fullmatch(self.source_container_name):
            raise ValueError("updater v2 源容器名称无效")
        if not REVISION_PATTERN.fullmatch(self.target_revision):
            raise ValueError("updater v2 目标修订无效")
        target_repository, separator, target_digest = self.target_image.rpartition("@")
        if (
            not separator
            or not DIGEST_PATTERN.fullmatch(target_digest)
            or target_repository
            not in {registry.repository for registry in OFFICIAL_REGISTRIES.values()}
        ):
            raise ValueError("updater v2 目标镜像无效")
        if self.candidate_token_hash is not None and not DIGEST_PATTERN.fullmatch(
            self.candidate_token_hash
        ):
            raise ValueError("updater v2 候选令牌哈希无效")
        if self.updated_at.tzinfo is None:
            raise ValueError("updater v2 更新时间必须包含时区")
        if self.error_code is not None and (
            not self.error_code
            or len(self.error_code) > 100
            or not self.error_code.replace("_", "").isalnum()
        ):
            raise ValueError("updater v2 错误代码无效")
        if (
            self.public_error_message is not None
            and len(self.public_error_message) > 1000
        ):
            raise ValueError("updater v2 公开错误信息过长")
        self._validate_status_checkpoint()
        return self

    def _validate_status_checkpoint(self) -> None:
        forward = self.checkpoint in FORWARD_CHECKPOINTS
        if forward == self.rollback_started:
            raise ValueError("updater v2 回滚标记与检查点不一致")
        if forward:
            allowed = {
                "snapshotting": set(FORWARD_CHECKPOINTS[:5]),
                "switching": set(FORWARD_CHECKPOINTS[5:8]),
                "verifying": {"candidate_started", "candidate_verified"},
                "commit_requested": {"commit_requested"},
                "success": {"commit_requested"},
                "failed": set(FORWARD_CHECKPOINTS),
            }
        else:
            allowed = {
                "rolling_back": set(ROLLBACK_CHECKPOINTS),
                "rolled_back": {"rollback_published"},
                "rollback_failed": set(ROLLBACK_CHECKPOINTS),
            }
        if self.checkpoint not in allowed.get(self.status, set()):
            raise ValueError("updater v2 状态与检查点不一致")

        if forward:
            checkpoint_index = FORWARD_CHECKPOINTS.index(self.checkpoint)
            token_required = checkpoint_index >= FORWARD_CHECKPOINTS.index("pending_ready")
            candidate_required = checkpoint_index >= FORWARD_CHECKPOINTS.index(
                "candidate_created"
            )
            if token_required != (self.candidate_token_hash is not None):
                raise ValueError("updater v2 候选令牌哈希与检查点不一致")
            if candidate_required != (self.candidate_container_id is not None):
                raise ValueError("updater v2 候选容器与检查点不一致")
        elif self.candidate_token_hash is None:
            raise ValueError("updater v2 回滚缺少候选令牌哈希")


class UpdaterResultJournal:
    def __init__(self, *, directory: str) -> None:
        self.directory = Path(directory)

    def start(self, *, operation_id: str) -> UpdaterResult:
        validate_operation_id(operation_id)
        if self._path(operation_id).exists():
            raise UpdateSnapshotError("updater 结果日志已存在")
        record = UpdaterResult(
            schema_version=1,
            operation_id=operation_id,
            sequence=1,
            status="snapshotting",
            updated_at=datetime.now(UTC),
        )
        self._write(record, replace=False)
        return record

    def start_v2(
        self,
        *,
        document: HandoffDocument,
        coordinator_container_id: str,
    ) -> UpdaterResultV2:
        validate_handoff_document(document)
        if self._path(document.operation_id).exists():
            raise UpdateSnapshotError("updater 结果日志已存在")
        try:
            record = UpdaterResultV2(
                schema_version=2,
                operation_id=document.operation_id,
                sequence=1,
                status="snapshotting",
                checkpoint="initialized",
                recovery_generation=0,
                coordinator_container_id=coordinator_container_id,
                source_container_id=document.current_container_id,
                source_image_id=document.source_image_id,
                source_container_name=document.candidate.name,
                target_image=document.target_image,
                target_revision=document.target_revision,
                rollback_started=False,
                updated_at=datetime.now(UTC),
            )
        except ValidationError as exc:
            raise UpdateSnapshotError("updater v2 初始结果无效") from exc
        self._write(record, replace=False)
        return record

    def read(self, *, operation_id: str) -> UpdaterResult | UpdaterResultV2:
        validate_operation_id(operation_id)
        path = self._path(operation_id)
        if path.is_symlink() or not path.is_file():
            raise UpdateSnapshotError("updater 结果日志不存在")
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_UPDATER_RESULT_BYTES:
                raise UpdateSnapshotError("updater 结果日志超过大小限制")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise UpdateSnapshotError("updater 结果日志无效")
            schema_version = payload.get("schema_version")
            if schema_version == 1:
                record: UpdaterResult | UpdaterResultV2 = UpdaterResult.model_validate(
                    payload
                )
            elif schema_version == 2:
                record = UpdaterResultV2.model_validate(payload)
            else:
                raise UpdateSnapshotError("updater 结果协议版本不受支持")
        except (OSError, ValidationError, ValueError) as exc:
            raise UpdateSnapshotError("updater 结果日志无效") from exc
        if record.operation_id != operation_id:
            raise UpdateSnapshotError("updater 结果日志操作标识不匹配")
        return record

    def transition(
        self,
        *,
        operation_id: str,
        status: str,
        error_code: str | None = None,
        public_error_message: str | None = None,
    ) -> UpdaterResult:
        current = self.read(operation_id=operation_id)
        if isinstance(current, UpdaterResultV2):
            raise UpdateSnapshotError("updater v2 结果必须使用检查点接口")
        if current.status in UPDATER_TERMINAL_STATUSES:
            raise UpdateSnapshotError("updater 终态结果不可修改")
        if status not in UPDATER_TRANSITIONS.get(current.status, set()):
            raise UpdateSnapshotError(
                f"updater 状态转换无效：{current.status} -> {status}"
            )
        validate_public_error(error_code, public_error_message)
        record = UpdaterResult(
            schema_version=1,
            operation_id=operation_id,
            sequence=current.sequence + 1,
            status=status,
            updated_at=datetime.now(UTC),
            error_code=error_code,
            public_error_message=public_error_message,
        )
        self._write(record, replace=True)
        return record

    def takeover_v2(
        self,
        *,
        operation_id: str,
        coordinator_container_id: str,
    ) -> UpdaterResultV2:
        current = self._read_v2(operation_id)
        if current.status in UPDATER_TERMINAL_STATUSES:
            raise UpdateSnapshotError("updater 终态结果不可修改")
        if current.recovery_generation >= MAX_RECOVERY_GENERATION:
            raise UpdateSnapshotError("updater 恢复代次已达上限")
        record = current.model_copy(
            update={
                "sequence": current.sequence + 1,
                "recovery_generation": current.recovery_generation + 1,
                "coordinator_container_id": coordinator_container_id,
                "updated_at": datetime.now(UTC),
            }
        )
        try:
            record = UpdaterResultV2.model_validate(record.model_dump())
        except ValidationError as exc:
            raise UpdateSnapshotError("updater v2 接管身份无效") from exc
        self._write(record, replace=True)
        return record

    def checkpoint_v2(
        self,
        *,
        operation_id: str,
        status: UpdaterStatus,
        checkpoint: UpdaterCheckpoint,
        candidate_token_hash: str | None = None,
        candidate_container_id: str | None = None,
        rollback_started: bool | None = None,
        error_code: str | None = None,
        public_error_message: str | None = None,
    ) -> UpdaterResultV2:
        current = self._read_v2(operation_id)
        if current.status in UPDATER_TERMINAL_STATUSES:
            raise UpdateSnapshotError("updater 终态结果不可修改")
        if status != current.status and status not in UPDATER_TRANSITIONS.get(
            current.status, set()
        ):
            raise UpdateSnapshotError(
                f"updater 状态转换无效：{current.status} -> {status}"
            )
        next_rollback_started = (
            current.rollback_started
            if rollback_started is None
            else rollback_started
        )
        if (
            current.candidate_token_hash is not None
            and candidate_token_hash is not None
            and candidate_token_hash != current.candidate_token_hash
        ):
            raise UpdateSnapshotError("updater v2 候选令牌哈希不可修改")
        if (
            current.candidate_container_id is not None
            and candidate_container_id is not None
            and candidate_container_id != current.candidate_container_id
        ):
            raise UpdateSnapshotError("updater v2 候选容器标识不可修改")
        try:
            self._validate_checkpoint_progress(
                current,
                status=status,
                checkpoint=checkpoint,
                rollback_started=next_rollback_started,
            )
        except ValueError as exc:
            raise UpdateSnapshotError("updater v2 检查点无效") from exc
        validate_public_error(error_code, public_error_message)
        record = current.model_copy(
            update={
                "sequence": current.sequence + 1,
                "status": status,
                "checkpoint": checkpoint,
                "candidate_token_hash": (
                    candidate_token_hash
                    if candidate_token_hash is not None
                    else current.candidate_token_hash
                ),
                "candidate_container_id": (
                    candidate_container_id
                    if candidate_container_id is not None
                    else current.candidate_container_id
                ),
                "rollback_started": next_rollback_started,
                "updated_at": datetime.now(UTC),
                "error_code": error_code,
                "public_error_message": public_error_message,
            }
        )
        try:
            record = UpdaterResultV2.model_validate(record.model_dump())
        except ValidationError as exc:
            raise UpdateSnapshotError("updater v2 检查点无效") from exc
        self._write(record, replace=True)
        return record

    def _read_v2(self, operation_id: str) -> UpdaterResultV2:
        current = self.read(operation_id=operation_id)
        if not isinstance(current, UpdaterResultV2):
            raise UpdateSnapshotError("updater v1 结果不支持 v2 写入")
        return current

    @staticmethod
    def _validate_checkpoint_progress(
        current: UpdaterResultV2,
        *,
        status: str,
        checkpoint: str,
        rollback_started: bool,
    ) -> None:
        current_rollback = current.checkpoint in ROLLBACK_CHECKPOINTS
        target_rollback = checkpoint in ROLLBACK_CHECKPOINTS
        if rollback_started != target_rollback:
            raise UpdateSnapshotError("updater v2 回滚检查点无效")
        if not current_rollback and target_rollback:
            if status != "rolling_back" or checkpoint != "rollback_started":
                raise UpdateSnapshotError("updater v2 必须从回滚起点进入回滚")
            return
        if current_rollback != target_rollback:
            raise UpdateSnapshotError("updater v2 检查点方向不可逆")
        checkpoints = ROLLBACK_CHECKPOINTS if target_rollback else FORWARD_CHECKPOINTS
        current_index = checkpoints.index(current.checkpoint)
        target_index = checkpoints.index(checkpoint)
        if target_index == current_index:
            if status == current.status:
                raise UpdateSnapshotError("updater v2 检查点没有推进")
            return
        if target_index != current_index + 1:
            raise UpdateSnapshotError("updater v2 检查点不可倒退或跳步")

    def _path(self, operation_id: str) -> Path:
        return self.directory / f"{operation_id}.json"

    def _write(
        self,
        record: UpdaterResult | UpdaterResultV2,
        *,
        replace: bool,
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        path = self._path(record.operation_id)
        temporary = self.directory / f".{record.operation_id}.result.tmp"
        temporary.unlink(missing_ok=True)
        write_private_json(temporary, record.model_dump(mode="json"))
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise UpdateSnapshotError("updater 结果日志已存在") from exc
            finally:
                temporary.unlink(missing_ok=True)
        fsync_directory(self.directory)


def validate_public_error(
    error_code: str | None,
    public_error_message: str | None,
) -> None:
    if error_code is not None and (
        not error_code
        or len(error_code) > 100
        or not error_code.replace("_", "").isalnum()
    ):
        raise UpdateSnapshotError("updater 错误代码无效")
    if public_error_message is not None and len(public_error_message) > 1000:
        raise UpdateSnapshotError("updater 公开错误信息过长")


def read_handoff(path: Path, *, expected_operation_id: str) -> HandoffDocument:
    validate_operation_id(expected_operation_id)
    if path.is_symlink() or not path.is_file():
        raise UpdateSnapshotError("updater handoff 文件无效")
    try:
        raw = path.read_bytes()
        if len(raw) > 1024 * 1024:
            raise UpdateSnapshotError("updater handoff 文件过大")
        document = HandoffDocument.model_validate_json(raw)
    except (OSError, ValidationError, ValueError) as exc:
        raise UpdateSnapshotError("updater handoff 内容无效") from exc
    if document.operation_id != expected_operation_id:
        raise UpdateSnapshotError("updater handoff 操作标识不匹配")
    validate_handoff_document(document)
    return document


def validate_handoff_document(document: HandoffDocument) -> None:
    if (
        not CONTAINER_ID_PATTERN.fullmatch(document.current_container_id)
        or document.candidate.container_id != document.current_container_id
        or not CONTAINER_NAME_PATTERN.fullmatch(document.candidate.name)
        or not DIGEST_PATTERN.fullmatch(document.source_image_id)
        or not VERSION_PATTERN.fullmatch(document.source_version)
        or not VERSION_PATTERN.fullmatch(document.target_version)
        or not DIGEST_PATTERN.fullmatch(document.target_digest)
        or not REVISION_PATTERN.fullmatch(document.target_revision)
        or document.target_image
        != f"{document.target_image.rsplit('@', 1)[0]}@{document.target_digest}"
    ):
        raise UpdateSnapshotError("updater handoff 不变量校验失败")
    allowed_targets = {
        f"{registry.repository}@{document.target_digest}"
        for registry in OFFICIAL_REGISTRIES.values()
    }
    if document.target_image not in allowed_targets:
        raise UpdateSnapshotError("updater handoff 目标镜像不属于官方仓库")
    if document.source_digest is not None and not DIGEST_PATTERN.fullmatch(
        document.source_digest
    ):
        raise UpdateSnapshotError("updater handoff 源镜像 digest 无效")
    data_mounts = [mount for mount in document.candidate.mounts if mount.target == "/data"]
    if len(data_mounts) != 1 or data_mounts[0].read_only:
        raise UpdateSnapshotError("updater handoff 数据挂载无效")


class UpdateSnapshotService:
    def __init__(self, *, data_directory: str) -> None:
        self.data_directory = Path(data_directory).resolve()
        self.backup_root = self.data_directory / "backups" / "updates"

    def create(self, *, operation_id: str, handoff_path: Path) -> Path:
        validate_operation_id(operation_id)
        expected_handoff = (
            self.data_directory
            / "update"
            / "operations"
            / f"{operation_id}.handoff.json"
        )
        if handoff_path.resolve() != expected_handoff.resolve():
            raise UpdateSnapshotError("updater handoff 路径无效")
        document = read_handoff(
            handoff_path,
            expected_operation_id=operation_id,
        )
        final_directory = self.backup_root / operation_id
        if final_directory.exists():
            raise UpdateSnapshotError("该更新操作的有效快照已存在")
        self.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.backup_root.chmod(0o700)
        temporary_directory = self.backup_root / f".{operation_id}.tmp"
        if temporary_directory.exists():
            shutil.rmtree(temporary_directory)
        temporary_directory.mkdir(mode=0o700)
        try:
            source_paths = self._source_paths(handoff_path)
            files = tuple(
                self._copy_snapshot_file(source, snapshot_name, temporary_directory)
                for source, snapshot_name in source_paths
            )
            manifest = SnapshotManifest(
                schema_version=SNAPSHOT_SCHEMA_VERSION,
                operation_id=operation_id,
                created_at=datetime.now(UTC),
                alembic_revision=read_alembic_revision(
                    self.data_directory / "mediasync.db"
                ),
                source_image_id=document.source_image_id,
                source_image_reference=document.source_image_reference,
                source_version=document.source_version,
                source_digest=document.source_digest,
                files=files,
            )
            write_private_json(
                temporary_directory / MANIFEST_FILENAME,
                manifest.model_dump(mode="json"),
            )
            fsync_directory(temporary_directory)
            os.replace(temporary_directory, final_directory)
            fsync_directory(self.backup_root)
        except BaseException:
            shutil.rmtree(temporary_directory, ignore_errors=True)
            raise
        return final_directory

    def restore(self, *, operation_id: str) -> SnapshotManifest:
        snapshot_directory, manifest = self.verify(operation_id=operation_id)
        temporary_files: dict[str, Path] = {}
        try:
            for item in manifest.files:
                if item.source_path == "update/handoff.json":
                    continue
                destination = self.data_directory / item.source_path
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = destination.with_name(f".{destination.name}.{operation_id}.restore")
                temporary.unlink(missing_ok=True)
                copy_file(snapshot_directory / item.snapshot_name, temporary)
                temporary_files[item.source_path] = temporary

            restored_paths = set(temporary_files)
            for optional in ("mediasync.db-wal", "mediasync.db-shm"):
                if optional not in restored_paths:
                    (self.data_directory / optional).unlink(missing_ok=True)
            for source_path, temporary in temporary_files.items():
                destination = self.data_directory / source_path
                os.replace(temporary, destination)
                destination.chmod(0o600)
                fsync_directory(destination.parent)
        except BaseException:
            for temporary in temporary_files.values():
                temporary.unlink(missing_ok=True)
            raise
        return manifest

    def verify(self, *, operation_id: str) -> tuple[Path, SnapshotManifest]:
        validate_operation_id(operation_id)
        directory = self.backup_root / operation_id
        manifest_path = directory / MANIFEST_FILENAME
        if directory.is_symlink() or not directory.is_dir() or not manifest_path.is_file():
            raise UpdateSnapshotError("更新快照不完整")
        try:
            manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise UpdateSnapshotError("更新快照 manifest 无效") from exc
        if manifest.operation_id != operation_id:
            raise UpdateSnapshotError("更新快照操作标识不匹配")
        required = {"mediasync.db", "config/runtime-secrets.json", "update/handoff.json"}
        present = {item.source_path for item in manifest.files}
        if not required.issubset(present) or len(present) != len(manifest.files):
            raise UpdateSnapshotError("更新快照文件清单无效")
        allowed_files = {
            "mediasync.db": "database.sqlite",
            "mediasync.db-wal": "database.sqlite-wal",
            "mediasync.db-shm": "database.sqlite-shm",
            "config/runtime-secrets.json": "runtime-secrets.json",
            "update/handoff.json": "handoff.json",
        }
        for item in manifest.files:
            if allowed_files.get(item.source_path) != item.snapshot_name:
                raise UpdateSnapshotError("更新快照文件映射无效")
            path = directory / item.snapshot_name
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != item.size
                or sha256_file(path) != item.sha256
            ):
                raise UpdateSnapshotError("更新快照文件校验失败")
        snapshot_revision = read_alembic_revision(directory / "database.sqlite")
        if snapshot_revision != manifest.alembic_revision:
            raise UpdateSnapshotError("更新快照 Alembic revision 不匹配")
        return directory, manifest

    def _source_paths(self, handoff_path: Path) -> list[tuple[Path, str]]:
        database = self.data_directory / "mediasync.db"
        secrets_path = self.data_directory / "config" / "runtime-secrets.json"
        required = ((database, "database.sqlite"), (secrets_path, "runtime-secrets.json"))
        for path, _ in required:
            validate_source_file(path)
        validate_source_file(handoff_path)
        sources = list(required)
        for suffix, name in (("-wal", "database.sqlite-wal"), ("-shm", "database.sqlite-shm")):
            path = Path(f"{database}{suffix}")
            if path.exists():
                validate_source_file(path)
                sources.append((path, name))
        sources.append((handoff_path, "handoff.json"))
        return sources

    def _copy_snapshot_file(
        self,
        source: Path,
        snapshot_name: str,
        directory: Path,
    ) -> SnapshotFile:
        before = source.stat()
        destination = directory / snapshot_name
        copy_file(source, destination)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise UpdateSnapshotError("创建快照期间源文件发生变化")
        return SnapshotFile(
            source_path=(
                "update/handoff.json"
                if snapshot_name == "handoff.json"
                else relative_source_path(source, self.data_directory)
            ),
            snapshot_name=snapshot_name,
            size=destination.stat().st_size,
            sha256=sha256_file(destination),
        )


def read_alembic_revision(database_path: Path) -> str:
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise UpdateSnapshotError("无法读取数据库 Alembic revision") from exc
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise UpdateSnapshotError("数据库 Alembic revision 无效")
    return row[0]


def validate_source_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise UpdateSnapshotError(f"快照源文件不存在或类型无效：{path.name}")


def relative_source_path(path: Path, data_directory: Path) -> str:
    try:
        return path.resolve().relative_to(data_directory).as_posix()
    except ValueError:
        return "update/handoff.json"


def copy_file(source: Path, destination: Path) -> None:
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
        shutil.copyfileobj(reader, writer, length=FILE_CHUNK_SIZE)
        writer.flush()
        os.fsync(writer.fileno())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(FILE_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
