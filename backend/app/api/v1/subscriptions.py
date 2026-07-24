from datetime import UTC

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.models import CloudAccount, Subscription, Task
from app.models.base import utcnow
from app.scheduler.manager import remove_subscription_job, upsert_subscription_job
from app.schemas.common import MessageResponse, Page
from app.schemas.subscription import SubscriptionCreate, SubscriptionRead, SubscriptionUpdate
from app.schemas.task import TaskRead
from app.services.scan_service import run_scan_by_id
from app.services.subscription_service import create_subscription, update_subscription
from app.services.task_enqueue_service import enqueue_manual_scan

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def _legacy_execution_enabled() -> bool:
    return get_settings().background_execution_mode == "legacy"


def _upsert_legacy_subscription_job(subscription: Subscription) -> None:
    if _legacy_execution_enabled():
        upsert_subscription_job(subscription)


def _remove_legacy_subscription_job(subscription_id: int) -> None:
    if _legacy_execution_enabled():
        remove_subscription_job(subscription_id)


def _get_subscription(db: DbSession, subscription_id: int) -> Subscription:
    subscription = db.get(Subscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return subscription


@router.get("", response_model=Page[SubscriptionRead])
def list_subscriptions(
    db: DbSession,
    _: AdminUser,
    page: int = 1,
    page_size: int = 20,
    enabled: bool | None = None,
) -> Page[SubscriptionRead]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    conditions = []
    if enabled is not None:
        conditions.append(Subscription.enabled == enabled)
    total = db.scalar(select(func.count()).select_from(Subscription).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Subscription)
            .where(*conditions)
            .order_by(Subscription.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page(items=items, page=page, page_size=page_size, total=total)


@router.post("", response_model=SubscriptionRead, status_code=201)
async def add_subscription(
    payload: SubscriptionCreate, db: DbSession, _: AdminUser
) -> Subscription:
    account = db.get(CloudAccount, payload.cloud_account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Cloud account not found")
    try:
        subscription = await create_subscription(db, account, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Subscription already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _upsert_legacy_subscription_job(subscription)
    return subscription


@router.get("/{subscription_id}", response_model=SubscriptionRead)
def get_subscription(subscription_id: int, db: DbSession, _: AdminUser) -> Subscription:
    return _get_subscription(db, subscription_id)


@router.patch("/{subscription_id}", response_model=SubscriptionRead)
def patch_subscription(
    subscription_id: int,
    payload: SubscriptionUpdate,
    db: DbSession,
    _: AdminUser,
) -> Subscription:
    subscription = update_subscription(db, _get_subscription(db, subscription_id), payload)
    _upsert_legacy_subscription_job(subscription)
    return subscription


@router.delete("/{subscription_id}", response_model=MessageResponse)
def delete_subscription(subscription_id: int, db: DbSession, _: AdminUser) -> MessageResponse:
    subscription = _get_subscription(db, subscription_id)
    _remove_legacy_subscription_job(subscription.id)
    db.delete(subscription)
    db.commit()
    return MessageResponse(message="Subscription deleted; target files were not removed")


@router.post("/{subscription_id}/scan", response_model=TaskRead, status_code=202)
def scan_subscription(
    subscription_id: int,
    background_tasks: BackgroundTasks,
    db: DbSession,
    _: AdminUser,
    full: bool = False,
) -> Task:
    subscription = _get_subscription(db, subscription_id)
    if not subscription.enabled:
        raise HTTPException(status_code=409, detail="Subscription is disabled")
    if subscription.last_scanned_at:
        last_scanned_at = subscription.last_scanned_at
        if last_scanned_at.tzinfo is None:
            last_scanned_at = last_scanned_at.replace(tzinfo=UTC)
        remaining = (
            get_settings().manual_scan_cooldown_seconds
            - (utcnow() - last_scanned_at).total_seconds()
        )
        if remaining > 0:
            raise HTTPException(
                status_code=429,
                detail=f"请等待 {int(remaining) + 1} 秒后再手动扫描",
            )
    enqueue_result = enqueue_manual_scan(
        db,
        subscription,
        force_full=full,
    )
    task = enqueue_result.task
    db.commit()
    db.refresh(task)
    if _legacy_execution_enabled() and enqueue_result.created:
        background_tasks.add_task(run_scan_by_id, subscription.id, "manual", full)
    return task
