from datetime import UTC, datetime

from fastapi import APIRouter
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.models import CloudFile, FolderCheckpoint, Subscription, Task
from app.providers import list_provider_types

router = APIRouter(tags=["system"])


@router.get("/system/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/system/info")
def system_info(_: AdminUser) -> dict[str, object]:
    settings = get_settings()
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "scheduler_enabled": settings.scheduler_enabled,
        "providers": list_provider_types(),
    }


@router.get("/dashboard/summary")
def dashboard_summary(db: DbSession, _: AdminUser) -> dict[str, object]:
    settings = get_settings()
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    recent_tasks = list(db.scalars(select(Task).order_by(Task.created_at.desc()).limit(6)))
    return {
        "subscriptions": db.scalar(select(func.count()).select_from(Subscription)) or 0,
        "active_subscriptions": db.scalar(
            select(func.count()).select_from(Subscription).where(Subscription.enabled.is_(True))
        )
        or 0,
        "pending_files": db.scalar(
            select(func.count())
            .select_from(CloudFile)
            .where(CloudFile.status.in_(["pending", "saving"]))
        )
        or 0,
        "saved_files": db.scalar(
            select(func.count()).select_from(CloudFile).where(CloudFile.status == "saved")
        )
        or 0,
        "saved_today": db.scalar(
            select(func.count())
            .select_from(CloudFile)
            .where(CloudFile.status == "saved", CloudFile.saved_at >= today)
        )
        or 0,
        "failed_tasks": db.scalar(
            select(func.count()).select_from(Task).where(Task.status == "failed")
        )
        or 0,
        "running_tasks": db.scalar(
            select(func.count()).select_from(Task).where(Task.status.in_(["pending", "running"]))
        )
        or 0,
        "last_scanned_at": db.scalar(select(func.max(Subscription.last_scanned_at))),
        "last_full_scanned_at": db.scalar(select(func.max(Subscription.last_full_scanned_at))),
        "next_scan_at": db.scalar(
            select(func.min(Subscription.next_scan_at)).where(Subscription.enabled.is_(True))
        ),
        "folder_checkpoints": db.scalar(select(func.count()).select_from(FolderCheckpoint)) or 0,
        "request_guard": {
            "interval_seconds": settings.aliyundrive_request_interval_seconds,
            "jitter_seconds": settings.aliyundrive_request_jitter_seconds,
            "max_retries": settings.aliyundrive_request_max_retries,
            "schedule_jitter_seconds": settings.scheduler_jitter_seconds,
            "folder_scan_batch_size": settings.folder_scan_batch_size,
            "full_scan_interval_hours": settings.full_scan_interval_hours,
        },
        "recent_tasks": [
            {
                "id": task.id,
                "type": task.type,
                "status": task.status,
                "message": task.message,
                "subscription_name": task.subscription.name if task.subscription else None,
                "created_at": task.created_at,
            }
            for task in recent_tasks
        ],
    }
