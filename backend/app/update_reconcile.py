from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models import UpdateOperation
from app.repositories import UpdateOperationRepository
from app.services.candidate_evidence_service import (
    CandidateEvidenceError,
    read_candidate_evidence,
)
from app.services.update_execution_gate import PendingUpdateMarker, UpdateExecutionGate
from app.services.update_snapshot_service import (
    UpdaterResultJournal,
    UpdateSnapshotError,
    fsync_directory,
    read_alembic_revision,
)

logger = logging.getLogger(__name__)
RECONCILABLE_RESULTS = frozenset({"commit_requested", "success", "rolled_back"})


class UpdateReconciliationError(RuntimeError):
    pass


class UpdateTerminalReconciler:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        data_directory: Path,
        pending_path: Path,
        unlink: Callable[[Path], None] | None = None,
        allow_active_commit: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._data_directory = Path(data_directory)
        self._pending_path = Path(pending_path)
        self._operations_directory = self._data_directory / "update" / "operations"
        self._unlink = unlink or (lambda path: path.unlink(missing_ok=True))
        self._allow_active_commit = allow_active_commit

    def reconcile(self) -> bool:
        self._validate_directories()
        marker = self._read_marker()
        if marker is None:
            return False

        journal = UpdaterResultJournal(directory=str(self._operations_directory))
        result_path = self._operations_directory / f"{marker.operation_id}.json"
        if not result_path.exists():
            return False
        try:
            result = journal.read(operation_id=marker.operation_id)
        except UpdateSnapshotError as exc:
            raise UpdateReconciliationError(str(exc)) from exc
        if result.updated_at.tzinfo is None:
            raise UpdateReconciliationError("updater 结果时间必须包含时区")

        with self._session_factory() as session:
            repository = UpdateOperationRepository(session)
            operation = repository.get_by_operation_id(marker.operation_id)
            if operation is None:
                raise UpdateReconciliationError("终态结果对应的更新操作不存在")

            if operation.active_slot is None:
                if operation.status not in {"success", "rolled_back"}:
                    raise UpdateReconciliationError("更新操作已经以其他终态结束")
                expected_results = (
                    {"commit_requested", "success"}
                    if operation.status == "success"
                    else {"rolled_back"}
                )
                if result.status not in expected_results:
                    raise UpdateReconciliationError("数据库终态与 updater 结果不一致")
            elif result.status == "rollback_failed":
                logger.error(
                    "update_rollback_failed operation_id=%s manual_intervention_required=true",
                    marker.operation_id,
                )
                return False
            elif result.status in RECONCILABLE_RESULTS:
                if result.status == "success":
                    raise UpdateReconciliationError("活动更新不能直接接受 SUCCESS 结果")
                if result.status == "commit_requested" and not self._allow_active_commit:
                    return False
                terminal_status = (
                    "success" if result.status == "commit_requested" else result.status
                )
                if result.status == "commit_requested":
                    if operation.status != "verifying":
                        raise UpdateReconciliationError(
                            "updater 提交请求与活动更新状态不匹配"
                        )
                    self._validate_success_evidence(
                        marker,
                        operation.target_version,
                        operation.target_digest,
                    )
                else:
                    self._advance_rollback_state(repository, operation, marker)
                repository.finish(
                    operation,
                    status=terminal_status,
                    completed_at=result.updated_at,
                    error_code=result.error_code,
                    error_message=result.public_error_message,
                )
                session.commit()
            else:
                return False

        self._cleanup(marker.operation_id)
        return True

    @staticmethod
    def _advance_rollback_state(
        repository: UpdateOperationRepository,
        operation: UpdateOperation,
        marker: PendingUpdateMarker,
    ) -> None:
        if (
            operation.target_version != marker.target_version
            or operation.target_digest != marker.target_digest
        ):
            raise UpdateReconciliationError("回滚结果与更新目标不匹配")
        paths = {
            "handoff": ("snapshotting", "rolling_back"),
            "snapshotting": ("rolling_back",),
            "switching": ("rolling_back",),
            "verifying": ("rolling_back",),
            "rolling_back": (),
        }
        transitions = paths.get(operation.status)
        if transitions is None:
            raise UpdateReconciliationError("回滚终态与活动更新状态不匹配")
        for status in transitions:
            repository.transition_active(operation, status=status)

    def _read_marker(self) -> PendingUpdateMarker | None:
        marker = UpdateExecutionGate(pending_path=str(self._pending_path)).read_pending_marker()
        if marker is None:
            return None
        if isinstance(marker, str):
            raise UpdateReconciliationError(marker)
        return marker

    def _validate_success_evidence(
        self,
        marker: PendingUpdateMarker,
        target_version: str | None,
        target_digest: str | None,
    ) -> None:
        evidence_path = self._operations_directory / f"{marker.operation_id}.candidate.json"
        try:
            evidence = read_candidate_evidence(
                evidence_path,
                expected_operation_id=marker.operation_id,
                expected_candidate_token=marker.candidate_token,
            )
        except CandidateEvidenceError as exc:
            raise UpdateReconciliationError(str(exc)) from exc
        if (
            evidence.version != marker.target_version.removeprefix("v")
            or evidence.revision != marker.target_revision
            or evidence.digest != marker.target_digest
            or target_version != marker.target_version
            or target_digest != marker.target_digest
            or evidence.alembic_revision
            != self._read_current_alembic_revision()
        ):
            raise UpdateReconciliationError("候选验证证据与目标更新不匹配")

    def _read_current_alembic_revision(self) -> str:
        try:
            return read_alembic_revision(self._data_directory / "mediasync.db")
        except UpdateSnapshotError as exc:
            raise UpdateReconciliationError(str(exc)) from exc

    def _validate_directories(self) -> None:
        update_directory = self._data_directory / "update"
        for path in (update_directory, self._operations_directory):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise UpdateReconciliationError("更新运行目录不安全")

    def _cleanup(self, operation_id: str) -> None:
        try:
            for suffix in ("candidate.json", "handoff.json"):
                self._unlink(self._operations_directory / f"{operation_id}.{suffix}")
            if self._operations_directory.exists():
                fsync_directory(self._operations_directory)
            self._unlink(self._pending_path)
            if self._pending_path.parent.exists():
                fsync_directory(self._pending_path.parent)
        except OSError as exc:
            raise UpdateReconciliationError("更新终态已提交，但运行标记清理失败") from exc


def main() -> int:
    from app.core.database import SessionLocal

    settings = get_settings()
    reconciler = UpdateTerminalReconciler(
        session_factory=SessionLocal,
        data_directory=Path("/data"),
        pending_path=Path(settings.update_pending_path),
    )
    try:
        reconciler.reconcile()
    except UpdateReconciliationError:
        logger.exception("update_terminal_reconciliation_failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
