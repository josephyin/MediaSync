from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.services.image_target_service import (
    DIGEST_PATTERN,
    OFFICIAL_REGISTRIES,
    VERSION_PATTERN,
)
from app.services.updater_handoff_service import (
    CONTAINER_ID_PATTERN,
    CONTAINER_NAME_PATTERN,
    validate_operation_id,
)

SNAPSHOT_SCHEMA_VERSION = 1
MANIFEST_FILENAME = "manifest.json"
FILE_CHUNK_SIZE = 1024 * 1024
UPDATER_TERMINAL_STATUSES = frozenset(
    {"success", "failed", "rolled_back", "rollback_failed"}
)
UPDATER_TRANSITIONS = {
    "snapshotting": {"switching", "rolling_back", "failed"},
    "switching": {"verifying", "rolling_back"},
    "verifying": {"success", "rolling_back"},
    "rolling_back": {"rolled_back", "rollback_failed"},
}


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
    schema_version: Literal[1]
    operation_id: str
    current_container_id: str
    source_image_id: str
    source_image_reference: str
    source_version: str
    source_digest: str | None
    target_version: str
    target_digest: str
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
        "rolling_back",
        "success",
        "failed",
        "rolled_back",
        "rollback_failed",
    ]
    updated_at: datetime
    error_code: str | None = None
    public_error_message: str | None = None


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

    def read(self, *, operation_id: str) -> UpdaterResult:
        validate_operation_id(operation_id)
        path = self._path(operation_id)
        if path.is_symlink() or not path.is_file():
            raise UpdateSnapshotError("updater 结果日志不存在")
        try:
            record = UpdaterResult.model_validate_json(path.read_bytes())
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
        if current.status in UPDATER_TERMINAL_STATUSES:
            raise UpdateSnapshotError("updater 终态结果不可修改")
        if status not in UPDATER_TRANSITIONS.get(current.status, set()):
            raise UpdateSnapshotError(
                f"updater 状态转换无效：{current.status} -> {status}"
            )
        if error_code is not None and (
            not error_code or len(error_code) > 100 or not error_code.replace("_", "").isalnum()
        ):
            raise UpdateSnapshotError("updater 错误代码无效")
        if public_error_message is not None and len(public_error_message) > 1000:
            raise UpdateSnapshotError("updater 公开错误信息过长")
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

    def _path(self, operation_id: str) -> Path:
        return self.directory / f"{operation_id}.json"

    def _write(self, record: UpdaterResult, *, replace: bool) -> None:
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
