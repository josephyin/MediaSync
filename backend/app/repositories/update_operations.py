from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.update_operation import (
    TERMINAL_UPDATE_OPERATION_STATUSES,
    UPDATE_OPERATION_STATUSES,
    UpdateOperation,
)


class ActiveUpdateOperationConflictError(RuntimeError):
    pass


class UpdateOperationStateError(RuntimeError):
    pass


ALLOWED_UPDATE_TRANSITIONS = {
    "checking": {"available", "failed", "cancelled"},
    "available": {"pulling", "failed", "cancelled"},
    "pulling": {"draining", "failed", "cancelled"},
    "draining": {"handoff", "failed", "cancelled"},
    "handoff": {"snapshotting", "failed"},
    "snapshotting": {"switching", "rolling_back"},
    "switching": {"verifying", "rolling_back"},
    "verifying": {"success", "rolling_back"},
    "rolling_back": {"rolled_back", "rollback_failed"},
}


class UpdateOperationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        source_version: str,
        status: str = "draining",
        target_version: str | None = None,
        target_digest: str | None = None,
        operation_id: str | None = None,
    ) -> UpdateOperation:
        if status in TERMINAL_UPDATE_OPERATION_STATUSES or status not in UPDATE_OPERATION_STATUSES:
            raise ValueError("new update operation must use an active status")
        operation = UpdateOperation(
            operation_id=operation_id or str(uuid.uuid4()),
            status=status,
            active_slot="global",
            source_version=source_version,
            target_version=target_version,
            target_digest=target_digest,
        )
        try:
            with self._session.begin_nested():
                self._session.add(operation)
                self._session.flush()
        except IntegrityError as exc:
            raise ActiveUpdateOperationConflictError(
                "another update operation is already active"
            ) from exc
        return operation

    def get_active(self) -> UpdateOperation | None:
        return self._session.scalar(
            select(UpdateOperation)
            .where(UpdateOperation.active_slot == "global")
            .limit(1)
        )

    def finish(
        self,
        operation: UpdateOperation,
        *,
        status: str,
        completed_at: datetime | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> UpdateOperation:
        if operation.active_slot != "global":
            raise UpdateOperationStateError("terminal update operation cannot be reused")
        if status not in TERMINAL_UPDATE_OPERATION_STATUSES:
            raise ValueError("finish requires a terminal status")
        self._validate_transition(operation.status, status)
        operation.status = status
        operation.active_slot = None
        operation.completed_at = completed_at or utcnow()
        operation.error_code = error_code
        operation.error_message = error_message
        self._session.flush()
        return operation

    def transition_active(
        self,
        operation: UpdateOperation,
        *,
        status: str,
    ) -> UpdateOperation:
        if operation.active_slot != "global":
            raise UpdateOperationStateError("terminal update operation cannot be reused")
        if status in TERMINAL_UPDATE_OPERATION_STATUSES:
            raise ValueError("terminal transition must use finish")
        self._validate_transition(operation.status, status)
        operation.status = status
        self._session.flush()
        return operation

    @staticmethod
    def _validate_transition(current: str, target: str) -> None:
        if target not in ALLOWED_UPDATE_TRANSITIONS.get(current, set()):
            raise UpdateOperationStateError(
                f"invalid update operation transition: {current} -> {target}"
            )
