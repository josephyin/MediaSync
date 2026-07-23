import hashlib
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import PurePosixPath

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import CloudFile, FolderCheckpoint, Subscription, Task
from app.models.base import utcnow
from app.providers import get_provider
from app.providers.base import RemoteItem, ShareInfo
from app.scheduler.schedule import parse_schedule
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.services.subscription_service import decrypt_share_password


@dataclass(slots=True)
class ScanResult:
    discovered: int = 0
    folders_scanned: int = 0

    def add(self, other: "ScanResult") -> None:
        self.discovered += other.discovered
        self.folders_scanned += other.folders_scanned


def fingerprint_item(item: RemoteItem) -> str:
    parts = [
        item.remote_file_id,
        item.item_type,
        str(item.size or ""),
        item.content_hash or "",
        item.updated_at.isoformat() if item.updated_at else "",
    ]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _queue_transfer(db: Session, subscription: Subscription, file: CloudFile) -> None:
    key = f"transfer:{subscription.id}:{file.remote_file_id}:{file.fingerprint}"
    if db.scalar(select(Task.id).where(Task.idempotency_key == key)) is not None:
        return
    file.status = "pending"
    db.add(
        Task(
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            trigger_type="scheduled",
            status="pending",
            idempotency_key=key,
        )
    )


def _record_item(
    db: Session,
    subscription: Subscription,
    item: RemoteItem,
    relative_path: str,
    create_transfers: bool,
) -> tuple[int, FolderCheckpoint | None]:
    now = utcnow()
    fingerprint = fingerprint_item(item)
    file = db.scalar(
        select(CloudFile).where(
            CloudFile.subscription_id == subscription.id,
            CloudFile.remote_file_id == item.remote_file_id,
        )
    )
    is_new = file is None
    changed = file is not None and file.fingerprint != fingerprint
    if file is None:
        file = CloudFile(
            subscription_id=subscription.id,
            remote_file_id=item.remote_file_id,
            parent_remote_file_id=item.parent_id,
            filename=item.filename,
            relative_path=relative_path,
            item_type=item.item_type,
            size=item.size,
            content_hash=item.content_hash,
            fingerprint=fingerprint,
            status="discovered",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(file)
        db.flush()
    else:
        file.parent_remote_file_id = item.parent_id
        file.filename = item.filename
        file.relative_path = relative_path
        file.item_type = item.item_type
        file.size = item.size
        file.content_hash = item.content_hash
        file.fingerprint = fingerprint
        file.last_seen_at = now

    checkpoint = None
    if item.item_type == "folder":
        checkpoint = db.scalar(
            select(FolderCheckpoint).where(
                FolderCheckpoint.subscription_id == subscription.id,
                FolderCheckpoint.remote_folder_id == item.remote_file_id,
            )
        )
        if checkpoint is None:
            checkpoint = FolderCheckpoint(
                subscription_id=subscription.id,
                remote_folder_id=item.remote_file_id,
                relative_path=relative_path,
                last_seen_at=now,
            )
            db.add(checkpoint)
            db.flush()
        else:
            checkpoint.relative_path = relative_path
            checkpoint.last_seen_at = now
    elif create_transfers and (is_new or changed):
        _queue_transfer(db, subscription, file)

    return (1 if is_new else 0), checkpoint


async def _scan_folder_contents(
    db: Session,
    subscription: Subscription,
    provider: object,
    share: ShareInfo,
    parent_id: str,
    parent_path: PurePosixPath,
    create_transfers: bool,
) -> tuple[ScanResult, list[FolderCheckpoint]]:
    result = ScanResult(folders_scanned=1)
    child_folders: list[FolderCheckpoint] = []
    marker: str | None = None
    while True:
        page = await provider.list_share_items(share, parent_id, marker)  # type: ignore[attr-defined]
        for item in page.items:
            relative_path = str(parent_path / item.filename)
            discovered, checkpoint = _record_item(
                db,
                subscription,
                item,
                relative_path,
                create_transfers,
            )
            result.discovered += discovered
            if checkpoint is not None:
                child_folders.append(checkpoint)
        marker = page.next_marker
        if not marker:
            break
    return result, child_folders


async def _scan_folder_full(
    db: Session,
    subscription: Subscription,
    provider: object,
    share: ShareInfo,
    parent_id: str,
    parent_path: PurePosixPath,
    create_transfers: bool,
) -> ScanResult:
    result, child_folders = await _scan_folder_contents(
        db,
        subscription,
        provider,
        share,
        parent_id,
        parent_path,
        create_transfers,
    )
    for checkpoint in child_folders:
        child_result = await _scan_folder_full(
            db,
            subscription,
            provider,
            share,
            checkpoint.remote_folder_id,
            PurePosixPath(checkpoint.relative_path),
            create_transfers,
        )
        checkpoint.last_scanned_at = utcnow()
        result.add(child_result)
    return result


async def _scan_folder_batch(
    db: Session,
    subscription: Subscription,
    provider: object,
    share: ShareInfo,
    root_folder_id: str,
    create_transfers: bool,
) -> ScanResult:
    result, _ = await _scan_folder_contents(
        db,
        subscription,
        provider,
        share,
        root_folder_id,
        PurePosixPath(""),
        create_transfers,
    )
    checkpoints = list(
        db.scalars(
            select(FolderCheckpoint)
            .where(FolderCheckpoint.subscription_id == subscription.id)
            .order_by(
                FolderCheckpoint.last_scanned_at.is_not(None),
                FolderCheckpoint.last_scanned_at,
                FolderCheckpoint.id,
            )
            .limit(get_settings().folder_scan_batch_size)
        )
    )
    for checkpoint in checkpoints:
        folder_result, _ = await _scan_folder_contents(
            db,
            subscription,
            provider,
            share,
            checkpoint.remote_folder_id,
            PurePosixPath(checkpoint.relative_path),
            create_transfers,
        )
        checkpoint.last_scanned_at = utcnow()
        result.add(folder_result)
    return result


def _full_scan_due(subscription: Subscription, force_full: bool) -> bool:
    if force_full or subscription.last_full_scanned_at is None:
        return True
    last_full = subscription.last_full_scanned_at
    if last_full.tzinfo is None:
        last_full = last_full.replace(tzinfo=UTC)
    return utcnow() - last_full >= timedelta(hours=get_settings().full_scan_interval_hours)


async def run_scan(
    db: Session,
    subscription: Subscription,
    trigger_type: str,
    force_full: bool = False,
) -> Task:
    existing_task = db.scalar(
        select(Task)
        .where(
            Task.subscription_id == subscription.id,
            Task.type == "scan",
            Task.status.in_(["pending", "running"]),
        )
        .order_by(Task.created_at.desc())
    )
    if existing_task and existing_task.status == "running":
        return existing_task
    task = existing_task or Task(
        subscription_id=subscription.id, type="scan", trigger_type=trigger_type
    )
    task.status = "running"
    task.started_at = utcnow()
    if existing_task is None:
        db.add(task)
    subscription.status = "scanning"
    db.commit()
    db.refresh(task)

    provider = None
    try:
        account = subscription.cloud_account
        provider = get_provider(account.provider, get_decrypted_token(account))
        password = decrypt_share_password(subscription)
        share = await provider.resolve_share(subscription.share_url, password)
        first_scan = subscription.last_scanned_at is None
        create_transfers = not (first_scan and subscription.initial_sync_mode == "future_only")
        full_scan = _full_scan_due(subscription, force_full)
        scan_started_at = utcnow()
        root_folder_id = subscription.source_folder_id or share.root_folder_id
        if full_scan:
            result = await _scan_folder_full(
                db,
                subscription,
                provider,
                share,
                root_folder_id,
                PurePosixPath(""),
                create_transfers,
            )
            db.execute(
                delete(FolderCheckpoint).where(
                    FolderCheckpoint.subscription_id == subscription.id,
                    FolderCheckpoint.last_seen_at < scan_started_at,
                )
            )
        else:
            result = await _scan_folder_batch(
                db,
                subscription,
                provider,
                share,
                root_folder_id,
                create_transfers,
            )
        now = utcnow()
        subscription.last_scanned_at = now
        if full_scan:
            subscription.last_full_scanned_at = now
        subscription.next_scan_at = now + parse_schedule(subscription.schedule).delta
        subscription.status = "active" if subscription.enabled else "disabled"
        subscription.last_error = None
        task.status = "success"
        request_count = getattr(provider, "request_count", None)
        request_summary = f"，API 请求 {request_count} 次" if request_count is not None else ""
        checkpoint_count = db.scalar(
            select(func.count())
            .select_from(FolderCheckpoint)
            .where(FolderCheckpoint.subscription_id == subscription.id)
        )
        mode = "完整校验" if full_scan else "增量轮询"
        task.message = (
            f"{mode}完成：检查 {result.folders_scanned} 个目录，"
            f"发现 {result.discovered} 个新增项目，目录检查点 {checkpoint_count or 0} 个"
            f"{request_summary}"
        )
        task.finished_at = now
        db.commit()
    except Exception as exc:
        now = utcnow()
        subscription.status = "error"
        subscription.last_error = str(exc)
        task.status = "failed"
        task.error_code = getattr(exc, "code", exc.__class__.__name__.upper())
        task.message = str(exc)
        task.finished_at = now
        db.commit()
    if provider is not None and persist_provider_token(subscription.cloud_account, provider):
        db.commit()
    db.refresh(task)
    return task


async def run_scan_by_id(
    subscription_id: int,
    trigger_type: str = "manual",
    force_full: bool = False,
) -> None:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        subscription = db.get(Subscription, subscription_id)
        if subscription and subscription.enabled:
            await run_scan(db, subscription, trigger_type, force_full)
