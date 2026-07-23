import random
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Task
from app.models.base import utcnow
from app.providers import get_provider
from app.providers.base import FolderRef, RemoteItem
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.services.subscription_service import decrypt_share_password


async def _target_folder(provider: object, root_path: str, relative_path: str) -> FolderRef:
    target = FolderRef(folder_id="root", path="/")
    full_path = PurePosixPath(root_path) / PurePosixPath(relative_path).parent
    for part in full_path.parts:
        if part not in {"", ".", "/"}:
            target = await provider.ensure_folder(target, part)  # type: ignore[attr-defined]
    return target


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
        share = await provider.resolve_share(
            subscription.share_url, decrypt_share_password(subscription)
        )
        target = await _target_folder(provider, subscription.target_path, file.relative_path)
        existing = await provider.find_target_item(target, file.filename)
        if existing:
            target_file_id = existing.remote_file_id
            target_path = str(PurePosixPath(target.path) / file.filename)
        else:
            source = RemoteItem(
                remote_file_id=file.remote_file_id,
                parent_id=file.parent_remote_file_id,
                filename=file.filename,
                item_type=file.item_type,
                size=file.size,
                content_hash=file.content_hash,
            )
            result = await provider.save_shared_item(share, source, target)
            target_file_id = result.target_file_id
            target_path = result.target_path
        now = utcnow()
        file.status = "saved"
        file.target_file_id = target_file_id
        file.target_path = target_path
        file.saved_at = now
        file.last_error = None
        task.status = "success"
        request_count = getattr(provider, "request_count", None)
        request_summary = f"，API 请求 {request_count} 次" if request_count is not None else ""
        task.message = f"已转存至 {target_path}{request_summary}"
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
