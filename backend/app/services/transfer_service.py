import random
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Task
from app.models.base import utcnow
from app.providers import get_provider
from app.providers.base import CloudDriveProvider, FolderRef
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.services.subscription_service import decrypt_share_password
from app.services.transfer_operation import (
    TransferSpec,
    ensure_target_folder,
    execute_transfer,
)


async def _target_folder(
    provider: CloudDriveProvider,
    root_path: str,
    relative_path: str,
) -> FolderRef:
    return await ensure_target_folder(provider, root_path, relative_path)


async def run_transfer(db: Session, task: Task) -> None:
    file = task.file
    subscription = task.subscription
    if file is None or subscription is None:
        task.status = "failed"
        task.message = "Transfer task is missing file or subscription"
        task.finished_at = utcnow()
        db.commit()
        return

    task.status = "running"
    task.started_at = utcnow()
    task.attempt_count += 1
    file.status = "saving"
    db.commit()

    provider = None
    try:
        account = subscription.cloud_account
        provider = get_provider(
            account.provider,
            get_decrypted_token(account),
            subscription.target_drive_id,
        )
        result = await execute_transfer(
            provider,
            TransferSpec(
                share_url=subscription.share_url,
                share_password=decrypt_share_password(subscription),
                target_path=subscription.target_path,
                remote_file_id=file.remote_file_id,
                parent_remote_file_id=file.parent_remote_file_id,
                filename=file.filename,
                relative_path=file.relative_path,
                item_type=file.item_type,
                size=file.size,
                content_hash=file.content_hash,
            ),
        )
        now = utcnow()
        file.status = "saved"
        file.target_file_id = result.target_file_id
        file.target_path = result.target_path
        file.saved_at = now
        file.last_error = None
        task.status = "success"
        request_count = getattr(provider, "request_count", None)
        request_summary = f"，API 请求 {request_count} 次" if request_count is not None else ""
        task.message = f"已转存至 {result.target_path}{request_summary}"
        task.finished_at = now
        task.next_attempt_at = None
    except Exception as exc:
        file.last_error = str(exc)
        task.error_code = getattr(exc, "code", exc.__class__.__name__.upper())
        task.message = str(exc)
        if task.attempt_count >= task.max_attempts:
            file.status = "failed"
            task.status = "failed"
            task.finished_at = utcnow()
            task.next_attempt_at = None
        else:
            settings = get_settings()
            delay = min(
                settings.transfer_retry_base_seconds * (2 ** (task.attempt_count - 1)),
                settings.transfer_retry_max_seconds,
            )
            delay += random.randint(0, min(delay // 4, 30))
            file.status = "pending"
            task.status = "pending"
            task.next_attempt_at = utcnow() + timedelta(seconds=delay)
    if provider is not None:
        persist_provider_token(subscription.cloud_account, provider)
    db.commit()


async def process_pending_transfers() -> None:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        now = utcnow()
        tasks = list(
            db.scalars(
                select(Task)
                .where(
                    Task.type == "transfer",
                    Task.status == "pending",
                    or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= now),
                )
                .order_by(Task.created_at)
                .limit(get_settings().transfer_batch_size)
            )
        )
        for task in tasks:
            await run_transfer(db, task)
