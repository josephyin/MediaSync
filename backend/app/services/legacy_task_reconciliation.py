from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Task
from app.repositories import TaskRepository
from app.task_engine.worker import ExponentialBackoff


@dataclass(frozen=True)
class LegacyTaskReconciliationResult:
    inspected_count: int = 0
    preserved_by_status: dict[str, int] = field(default_factory=dict)
    orphan_retry_count: int = 0
    orphan_failed_count: int = 0
    run_appended_count: int = 0
    run_finalized_count: int = 0
    run_preserved_count: int = 0


def reconcile_legacy_tasks(
    db: Session,
    *,
    reconciled_at: datetime,
    retry_backoff: ExponentialBackoff,
) -> LegacyTaskReconciliationResult:
    tasks = list(db.scalars(select(Task).order_by(Task.id.asc())))
    preserved_by_status: dict[str, int] = {}
    orphan_retry_count = 0
    orphan_failed_count = 0
    run_actions = {
        "appended": 0,
        "finalized": 0,
        "preserved": 0,
    }
    repository = TaskRepository(db, clock=lambda: reconciled_at)

    for task in tasks:
        if task.status != "running":
            preserved_by_status[task.status] = preserved_by_status.get(task.status, 0) + 1
            continue
        ownership_is_complete = all(
            value is not None
            for value in (
                task.locked_by,
                task.lock_token,
                task.locked_at,
                task.lease_until,
            )
        )
        if ownership_is_complete:
            preserved_by_status["running_v2_owned"] = (
                preserved_by_status.get("running_v2_owned", 0) + 1
            )
            continue

        next_retry_count = max(task.retry_count, task.attempt_count, 1)
        reconciled = repository.reconcile_legacy_orphan(
            task.id,
            next_attempt_at=reconciled_at + retry_backoff.delay_for(max(next_retry_count - 1, 0)),
            reconciled_at=reconciled_at,
        )
        if reconciled is None:
            preserved_by_status["running_concurrent_change"] = (
                preserved_by_status.get("running_concurrent_change", 0) + 1
            )
            continue
        if reconciled.task.status == "retry":
            orphan_retry_count += 1
        else:
            orphan_failed_count += 1
        run_actions[reconciled.run_action] += 1

    return LegacyTaskReconciliationResult(
        inspected_count=len(tasks),
        preserved_by_status=preserved_by_status,
        orphan_retry_count=orphan_retry_count,
        orphan_failed_count=orphan_failed_count,
        run_appended_count=run_actions["appended"],
        run_finalized_count=run_actions["finalized"],
        run_preserved_count=run_actions["preserved"],
    )
