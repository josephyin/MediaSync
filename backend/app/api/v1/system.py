from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.models import CloudFile, FolderCheckpoint, Subscription, Task
from app.providers import list_provider_types
from app.repositories import ActiveUpdateOperationConflictError, UpdateOperationRepository
from app.schemas.update import UpdateInstallRequest, UpdateOperationInfo, UpdateStatusRead
from app.services.docker_capability_service import get_docker_capability_service
from app.services.update_check_service import get_update_check_service, parse_version
from app.services.update_execution_gate import build_update_execution_gate
from app.services.update_install_service import get_update_install_service

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


@router.get("/system/update", response_model=UpdateStatusRead)
async def update_status(db: DbSession, _: AdminUser) -> UpdateStatusRead:
    status = get_update_check_service().get_status()
    return await _with_runtime_status(status, db)


@router.post("/system/update/check", response_model=UpdateStatusRead)
async def check_for_updates(db: DbSession, _: AdminUser) -> UpdateStatusRead:
    status = await get_update_check_service().check()
    return await _with_runtime_status(status, db)


@router.post("/system/update/install", response_model=UpdateStatusRead, status_code=202)
async def install_update(
    payload: UpdateInstallRequest,
    background_tasks: BackgroundTasks,
    db: DbSession,
    _: AdminUser,
) -> UpdateStatusRead:
    checked = get_update_check_service().get_status()
    capability = await get_docker_capability_service().probe()
    latest = checked.latest_release
    if capability.reason_code != "ready":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{capability.message}，无法启用一键更新",
        )
    if checked.status != "update_available" or latest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="当前没有可以安装的新版本，请先检查更新",
        )
    if payload.target_version != latest.tag_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="目标版本已经变化，请重新检查更新",
        )
    service = get_update_install_service()
    try:
        operation = service.begin(
            db,
            source_version=checked.current_version,
            target_version=latest.tag_name,
        )
    except ActiveUpdateOperationConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已有更新正在执行",
        ) from exc
    background_tasks.add_task(
        service.execute,
        operation_id=operation.operation_id,
        target_version=latest.tag_name,
    )
    return await _with_runtime_status(checked, db)


async def _with_runtime_status(
    status: UpdateStatusRead,
    db: DbSession,
) -> UpdateStatusRead:
    capability = await get_docker_capability_service().probe()
    gate = build_update_execution_gate().evaluate(db)
    repository = UpdateOperationRepository(db)
    active_operation = repository.get_active()
    operation = active_operation
    if operation is None:
        latest_operation = repository.get_latest()
        if latest_operation is not None and _terminal_operation_matches_current_version(
            status=latest_operation.status,
            source_version=latest_operation.source_version,
            target_version=latest_operation.target_version,
            current_version=status.current_version,
        ):
            operation = latest_operation
    install_supported = (
        capability.reason_code == "ready"
        and active_operation is None
        and status.status == "update_available"
        and status.latest_release is not None
    )
    if active_operation is not None:
        reason = "已有更新正在执行，请等待当前操作完成"
    elif capability.reason_code != "ready":
        reason = f"{capability.message}；请继续通过 NAS 容器管理器手动升级"
    elif status.status != "update_available":
        reason = "当前没有可以安装的新版本"
    else:
        reason = None
    return status.model_copy(
        update={
            "docker_socket_enabled": capability.socket_available,
            "docker_capability": capability,
            "install_supported": install_supported,
            "install_unavailable_reason": reason,
            "runtime_mode": gate.mode,
            "operation": (
                UpdateOperationInfo.model_validate(operation, from_attributes=True)
                if operation is not None
                else None
            ),
        }
    )


def _terminal_operation_matches_current_version(
    *,
    status: str,
    source_version: str,
    target_version: str | None,
    current_version: str,
) -> bool:
    current = parse_version(current_version)
    source = parse_version(source_version)
    target = parse_version(target_version) if target_version is not None else None
    if current is None:
        return False
    if status == "success":
        return target == current
    if status in {"failed", "cancelled", "rolled_back"}:
        return source == current
    if status == "rollback_failed":
        return source == current or target == current
    return False


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
