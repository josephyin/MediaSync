from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Subscription, Task
from app.models.base import utcnow
from app.repositories import TaskRepository
from app.scheduler.schedule import parse_schedule
from app.task_engine import TERMINAL_TASK_STATUSES


@dataclass(frozen=True)
class ScheduledScanEnqueueResult:
    inspected_count: int
    enqueued_count: int
    skipped_active_count: int
    task_ids: tuple[int, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _scheduled_scan_key(subscription_id: int, due_at: datetime) -> str:
    slot = _as_utc(due_at).isoformat(timespec="microseconds")
    return f"scan:{subscription_id}:scheduled:{slot}"


def enqueue_due_scan_tasks(
    session: Session,
    *,
    scheduled_at: datetime | None = None,
    limit: int = 100,
) -> ScheduledScanEnqueueResult:
    """Enqueue due Scan v1 tasks in the caller-owned database transaction.

    This operation deliberately does not commit. The caller must commit the
    task inserts and subscription schedule changes together.
    """

    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")

    now = _as_utc(scheduled_at or utcnow())
    subscriptions = list(
        session.scalars(
            select(Subscription)
            .where(
                Subscription.enabled.is_(True),
                Subscription.next_scan_at.is_not(None),
                Subscription.next_scan_at <= now,
            )
            .order_by(Subscription.next_scan_at, Subscription.id)
            .limit(limit)
        )
    )
    repository = TaskRepository(session)
    task_ids: list[int] = []
    skipped_active_count = 0

    for subscription in subscriptions:
        due_at = subscription.next_scan_at
        if due_at is None:
            continue

        subscription.next_scan_at = now + parse_schedule(subscription.schedule).delta
        active_task_id = session.scalar(
            select(Task.id)
            .where(
                Task.subscription_id == subscription.id,
                Task.type == "scan",
                Task.status.not_in(TERMINAL_TASK_STATUSES),
            )
            .limit(1)
        )
        if active_task_id is not None:
            skipped_active_count += 1
            continue

        task = repository.create_task(
            Task(
                account_id=subscription.cloud_account_id,
                subscription_id=subscription.id,
                type="scan",
                trigger_type="scheduled",
                status="pending",
                payload_version=1,
                payload={"force_full": False},
                idempotency_key=_scheduled_scan_key(subscription.id, due_at),
            )
        )
        task_ids.append(task.id)

    return ScheduledScanEnqueueResult(
        inspected_count=len(subscriptions),
        enqueued_count=len(task_ids),
        skipped_active_count=skipped_active_count,
        task_ids=tuple(task_ids),
    )
