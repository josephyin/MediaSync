from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CloudFile, Subscription, Task
from app.repositories import TaskRepository
from app.task_engine import TERMINAL_TASK_STATUSES


@dataclass(frozen=True, slots=True)
class TaskEnqueueResult:
    task: Task
    created: bool


def _active_task(
    db: Session,
    *,
    task_type: str,
    subscription_id: int | None = None,
    file_id: int | None = None,
) -> Task | None:
    conditions = [
        Task.type == task_type,
        Task.status.not_in(TERMINAL_TASK_STATUSES),
    ]
    if subscription_id is not None:
        conditions.append(Task.subscription_id == subscription_id)
    if file_id is not None:
        conditions.append(Task.file_id == file_id)
    return db.scalar(
        select(Task).where(*conditions).order_by(Task.created_at.desc(), Task.id.desc()).limit(1)
    )


def enqueue_manual_scan(
    db: Session,
    subscription: Subscription,
    *,
    force_full: bool,
) -> TaskEnqueueResult:
    existing = _active_task(
        db,
        task_type="scan",
        subscription_id=subscription.id,
    )
    if existing is not None:
        return TaskEnqueueResult(task=existing, created=False)
    return TaskEnqueueResult(
        task=TaskRepository(db).create_task(
            Task(
                account_id=subscription.cloud_account_id,
                subscription_id=subscription.id,
                type="scan",
                trigger_type="manual",
                status="pending",
                payload_version=1,
                payload={"force_full": force_full},
            )
        ),
        created=True,
    )


def enqueue_transfer_retry(db: Session, file: CloudFile) -> Task:
    existing = _active_task(
        db,
        task_type="transfer",
        file_id=file.id,
    )
    if existing is not None:
        return existing

    predecessor = db.scalar(
        select(Task)
        .where(
            Task.file_id == file.id,
            Task.type == "transfer",
            Task.status.in_(TERMINAL_TASK_STATUSES),
        )
        .order_by(Task.created_at.desc(), Task.id.desc())
        .limit(1)
    )
    predecessor_id = predecessor.id if predecessor is not None else 0
    subscription = file.subscription
    successor = TaskRepository(db).create_task(
        Task(
            account_id=subscription.cloud_account_id,
            subscription_id=file.subscription_id,
            file_id=file.id,
            type="transfer",
            trigger_type="retry",
            status="pending",
            payload_version=1,
            payload={},
            idempotency_key=f"transfer-retry:{file.id}:{predecessor_id}",
        )
    )
    file.status = "pending"
    file.last_error = None
    return successor
