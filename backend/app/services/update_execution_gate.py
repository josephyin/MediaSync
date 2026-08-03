from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.repositories import UpdateOperationRepository
from app.services.update_check_service import parse_version

MAX_PENDING_MARKER_BYTES = 16 * 1024
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CANDIDATE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
GATED_UPDATE_STATUSES = frozenset(
    {
        "draining",
        "handoff",
        "snapshotting",
        "switching",
        "verifying",
        "rolling_back",
    }
)


@dataclass(frozen=True)
class UpdateGateDecision:
    blocked: bool
    mode: str
    reason: str | None = None
    operation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingUpdateMarker:
    operation_id: str
    target_version: str
    target_digest: str
    target_revision: str
    candidate_token: str
    mode: str = "candidate_validation"


class UpdateExecutionGate:
    def __init__(self, *, pending_path: str) -> None:
        self._pending_path = Path(pending_path)

    def evaluate(self, session: Session) -> UpdateGateDecision:
        marker = self.read_pending_marker()
        if marker is not None:
            if isinstance(marker, str):
                return UpdateGateDecision(
                    blocked=True,
                    mode="candidate_invalid",
                    reason=marker,
                )
            active = UpdateOperationRepository(session).get_active()
            if active is None or active.operation_id != marker.operation_id:
                return UpdateGateDecision(
                    blocked=True,
                    mode="candidate_invalid",
                    reason="候选验证标记与活动更新操作不匹配",
                )
            return UpdateGateDecision(
                blocked=True,
                mode="candidate_validation",
                operation_id=marker.operation_id,
            )

        active = UpdateOperationRepository(session).get_active()
        if active is not None and active.status in GATED_UPDATE_STATUSES:
            return UpdateGateDecision(
                blocked=True,
                mode="draining",
                operation_id=active.operation_id,
            )
        return UpdateGateDecision(blocked=False, mode="normal")

    def pending_marker_present(self) -> bool:
        try:
            return self._pending_path.exists() or self._pending_path.is_symlink()
        except OSError:
            return True

    def read_pending_marker(self) -> PendingUpdateMarker | str | None:
        try:
            if not self._pending_path.exists():
                return None
            if self._pending_path.is_symlink() or not self._pending_path.is_file():
                return "候选验证标记路径不安全"
            if self._pending_path.stat().st_size > MAX_PENDING_MARKER_BYTES:
                return "候选验证标记超过大小限制"
            payload = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "候选验证标记无法安全解析"
        if not isinstance(payload, dict) or set(payload) != {
            "operation_id",
            "target_version",
            "target_digest",
            "target_revision",
            "candidate_token",
            "mode",
        }:
            return "候选验证标记字段无效"
        if payload.get("mode") != "candidate_validation":
            return "候选验证标记模式无效"
        operation_id = payload.get("operation_id")
        target_version = payload.get("target_version")
        target_digest = payload.get("target_digest")
        target_revision = payload.get("target_revision")
        candidate_token = payload.get("candidate_token")
        try:
            uuid.UUID(operation_id)
        except (AttributeError, TypeError, ValueError):
            return "候选验证操作标识无效"
        if not isinstance(target_version, str) or parse_version(target_version) is None:
            return "候选验证目标版本无效"
        if not isinstance(target_digest, str) or not DIGEST_PATTERN.fullmatch(
            target_digest
        ):
            return "候选验证目标摘要无效"
        if not isinstance(target_revision, str) or not REVISION_PATTERN.fullmatch(
            target_revision
        ):
            return "候选验证目标修订无效"
        if not isinstance(candidate_token, str) or not CANDIDATE_TOKEN_PATTERN.fullmatch(
            candidate_token
        ):
            return "候选验证令牌无效"
        return PendingUpdateMarker(
            operation_id=operation_id,
            target_version=target_version,
            target_digest=target_digest,
            target_revision=target_revision,
            candidate_token=candidate_token,
        )


def build_update_execution_gate() -> UpdateExecutionGate:
    return UpdateExecutionGate(pending_path=get_settings().update_pending_path)
