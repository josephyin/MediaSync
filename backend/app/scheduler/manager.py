from datetime import UTC

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.config import get_settings
from app.models import Subscription, Task
from app.scheduler.schedule import parse_schedule
from app.services.scan_service import run_scan_by_id
from app.services.transfer_service import process_pending_transfers

scheduler = AsyncIOScheduler(timezone=UTC)


def subscription_job_id(subscription_id: int) -> str:
    return f"subscription:{subscription_id}"


def upsert_subscription_job(subscription: Subscription) -> None:
    if not scheduler.running:
        return
    job_id = subscription_job_id(subscription.id)
    if not subscription.enabled:
        remove_subscription_job(subscription.id)
        return
    schedule = parse_schedule(subscription.schedule)
    scheduler.add_job(
        run_scan_by_id,
        "interval",
        seconds=int(schedule.delta.total_seconds()),
        args=[subscription.id, "scheduled"],
        id=job_id,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        jitter=get_settings().scheduler_jitter_seconds,
        next_run_time=subscription.next_scan_at,
    )


def remove_subscription_job(subscription_id: int) -> None:
    if scheduler.get_job(subscription_job_id(subscription_id)):
        scheduler.remove_job(subscription_job_id(subscription_id))


def start_scheduler() -> None:
    settings = get_settings()
    if not settings.scheduler_enabled or scheduler.running:
        return
    scheduler.start()
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        stale_tasks = list(db.scalars(select(Task).where(Task.status == "running")))
        for task in stale_tasks:
            task.status = "pending" if task.type == "transfer" else "failed"
            task.message = "应用重启后已恢复，等待重新执行"
            if task.type == "transfer":
                task.next_attempt_at = None
        db.commit()
        subscriptions = list(db.scalars(select(Subscription).where(Subscription.enabled.is_(True))))
        for subscription in subscriptions:
            upsert_subscription_job(subscription)
    scheduler.add_job(
        process_pending_transfers,
        "interval",
        seconds=settings.transfer_poll_seconds,
        id="transfer-worker",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
