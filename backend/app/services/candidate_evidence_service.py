from __future__ import annotations

import os
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.services.update_check_service import parse_version
from app.services.update_execution_gate import (
    CANDIDATE_TOKEN_PATTERN,
    DIGEST_PATTERN,
    REVISION_PATTERN,
    PendingUpdateMarker,
    UpdateExecutionGate,
)
from app.services.update_snapshot_service import (
    UpdateSnapshotError,
    fsync_directory,
    read_alembic_revision,
    write_private_json,
)

REQUIRED_COMPONENTS = frozenset({"launcher", "nginx", "api", "scheduler", "worker"})
MAX_CANDIDATE_EVIDENCE_BYTES = 32 * 1024
ALEMBIC_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class CandidateEvidenceError(RuntimeError):
    pass


def normalize_version(value: str) -> str:
    normalized = value.strip().removeprefix("v")
    if parse_version(normalized) is None:
        raise CandidateEvidenceError("候选应用版本无效")
    return normalized


class CandidateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    operation_id: str
    candidate_token: str
    mode: Literal["candidate_validation"]
    version: str
    revision: str
    digest: str
    alembic_revision: str
    components: dict[str, bool]
    observed_at: datetime

    @model_validator(mode="after")
    def validate_contract(self) -> CandidateEvidence:
        try:
            uuid.UUID(self.operation_id)
        except ValueError as exc:
            raise ValueError("候选操作标识无效") from exc
        if not CANDIDATE_TOKEN_PATTERN.fullmatch(self.candidate_token):
            raise ValueError("候选验证令牌无效")
        if parse_version(self.version) is None:
            raise ValueError("候选版本无效")
        if not REVISION_PATTERN.fullmatch(self.revision):
            raise ValueError("候选源码修订无效")
        if not DIGEST_PATTERN.fullmatch(self.digest):
            raise ValueError("候选镜像摘要无效")
        if not ALEMBIC_REVISION_PATTERN.fullmatch(self.alembic_revision):
            raise ValueError("候选数据库修订无效")
        if set(self.components) != REQUIRED_COMPONENTS or not all(self.components.values()):
            raise ValueError("候选组件健康状态不完整")
        if self.observed_at.tzinfo is None:
            raise ValueError("候选观察时间必须包含时区")
        return self


class CandidateEvidenceService:
    def __init__(
        self,
        *,
        data_directory: Path,
        pending_path: Path,
        environment: Mapping[str, str],
        app_version: str,
    ) -> None:
        self._data_directory = Path(data_directory)
        self._pending_path = Path(pending_path)
        self._environment = dict(environment)
        self._app_version = app_version
        self._written_for: tuple[str, str] | None = None

    def observe(self, components: Mapping[str, bool]) -> bool:
        marker = UpdateExecutionGate(pending_path=str(self._pending_path)).read_pending_marker()
        if marker is None:
            return False
        if isinstance(marker, str):
            raise CandidateEvidenceError(marker)
        identity = (marker.operation_id, marker.candidate_token)
        if self._written_for == identity:
            return True
        self._validate_candidate(marker, components)

        try:
            evidence = CandidateEvidence(
                schema_version=1,
                operation_id=marker.operation_id,
                candidate_token=marker.candidate_token,
                mode="candidate_validation",
                version=normalize_version(self._app_version),
                revision=marker.target_revision,
                digest=marker.target_digest,
                alembic_revision=self._read_alembic_revision(),
                components=dict(components),
                observed_at=datetime.now(UTC),
            )
        except ValidationError as exc:
            raise CandidateEvidenceError("无法生成有效候选验证证据") from exc
        self._write(evidence)
        self._written_for = identity
        return True

    def _validate_candidate(
        self,
        marker: PendingUpdateMarker,
        components: Mapping[str, bool],
    ) -> None:
        if set(components) != REQUIRED_COMPONENTS or not all(components.values()):
            raise CandidateEvidenceError("候选组件尚未全部健康")
        expected_environment = {
            "MEDIASYNC_CANDIDATE_TOKEN": marker.candidate_token,
            "MEDIASYNC_IMAGE_REVISION": marker.target_revision,
            "MEDIASYNC_IMAGE_DIGEST": marker.target_digest,
        }
        if any(self._environment.get(key) != value for key, value in expected_environment.items()):
            raise CandidateEvidenceError("候选镜像身份与待验证标记不匹配")
        if normalize_version(self._app_version) != normalize_version(marker.target_version):
            raise CandidateEvidenceError("候选应用版本与待验证标记不匹配")

        active = self._read_active_operation()
        if active != (
            marker.operation_id,
            "verifying",
            marker.target_version,
            marker.target_digest,
        ):
            raise CandidateEvidenceError("候选验证标记与活动更新操作不匹配")

    def _read_active_operation(self) -> tuple[str, str, str, str] | None:
        database_path = self._data_directory / "mediasync.db"
        try:
            connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT operation_id, status, target_version, target_digest "
                    "FROM update_operations WHERE active_slot = 'global'"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise CandidateEvidenceError("无法读取活动更新操作") from exc
        if len(rows) != 1 or not all(isinstance(value, str) for value in rows[0]):
            return None
        return tuple(rows[0])  # type: ignore[return-value]

    def _read_alembic_revision(self) -> str:
        try:
            return read_alembic_revision(self._data_directory / "mediasync.db")
        except UpdateSnapshotError as exc:
            raise CandidateEvidenceError(str(exc)) from exc

    def _write(self, evidence: CandidateEvidence) -> None:
        update_directory = self._data_directory / "update"
        directory = update_directory / "operations"
        try:
            update_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if update_directory.is_symlink() or not update_directory.is_dir():
                raise CandidateEvidenceError("候选更新目录不安全")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise CandidateEvidenceError("候选证据目录不安全")
            os.chmod(directory, 0o700)
            destination = directory / f"{evidence.operation_id}.candidate.json"
            if destination.is_symlink():
                raise CandidateEvidenceError("候选证据路径不安全")
            temporary = directory / f".{evidence.operation_id}.{uuid.uuid4().hex}.tmp"
            write_private_json(temporary, evidence.model_dump(mode="json"))
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
            fsync_directory(directory)
        except CandidateEvidenceError:
            raise
        except OSError as exc:
            raise CandidateEvidenceError("无法持久化候选验证证据") from exc


def read_candidate_evidence(
    path: Path,
    *,
    expected_operation_id: str,
    expected_candidate_token: str,
) -> CandidateEvidence:
    try:
        if path.is_symlink() or not path.is_file():
            raise CandidateEvidenceError("候选验证证据不存在")
        if path.stat().st_size > MAX_CANDIDATE_EVIDENCE_BYTES:
            raise CandidateEvidenceError("候选验证证据超过大小限制")
        evidence = CandidateEvidence.model_validate_json(path.read_bytes())
    except CandidateEvidenceError:
        raise
    except (OSError, ValidationError, ValueError) as exc:
        raise CandidateEvidenceError("候选验证证据无效") from exc
    if (
        evidence.operation_id != expected_operation_id
        or evidence.candidate_token != expected_candidate_token
    ):
        raise CandidateEvidenceError("候选验证证据归属不匹配")
    return evidence
