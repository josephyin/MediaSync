"""Local downloads share the task queue, but never change cloud transfer state."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import or_, select

from app.core.config import get_settings
from app.models import Task
from app.models.base import utcnow
from app.providers import get_provider
from app.providers.pikpak.errors import PikPakCredentialError, PikPakRequestError
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.services.download_stream import (
    DownloadDeferred,
    DownloadError,
    stream_download,
)
from app.services.download_stream import (
    validate_download_url as validate_download_url,
)
from app.task_engine.handlers import TaskOutcome


def destination_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or any(x in ("..", ".") for x in relative.split("/")):
        raise DownloadError("下载路径必须是根目录下的相对路径，不能包含 . 或 ..")
    if "\\" in relative or "\x00" in relative:
        raise DownloadError("下载路径包含无效字符")
    if not root.is_dir():
        raise DownloadError("下载根目录不存在，请先创建目录或挂载 NAS")
    root = root.resolve()
    target = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise DownloadError("下载路径不能经过符号链接")
    if not target.resolve().is_relative_to(root):
        raise DownloadError("下载路径超出根目录")
    return target


async def execute_download(task_id, session_factory, *, context=None):
    provider = None
    account_id = None
    try:
        with session_factory() as db:
            task = db.get(Task, task_id)
            if task is None or task.file is None or task.subscription is None:
                raise DownloadError("下载文件记录不存在")
            file = task.file
            account = task.subscription.cloud_account
            if account.provider != "pikpak" or file.status != "saved" or not file.target_file_id:
                raise DownloadError("目前支持下载 PikPak 已转存的文件")
            relative = str(task.payload.get("relative_path", ""))
            root = Path(str(task.payload.get("root") or get_settings().download_root)).expanduser()
            target = destination_path(
                root,
                relative,
            )
            root_identity = task.payload.get("root_identity")

            def guard():
                stat = root.stat()
                if root_identity and [stat.st_dev, stat.st_ino] != root_identity:
                    raise DownloadError("下载目录挂载已变化，请检查 NAS 后重新选择目录")
                destination_path(root, relative)

            guard()
            expected_size = file.size
            expected_hash = file.content_hash
            remote_id = file.target_file_id
            account_id = account.id
            provider = get_provider(account.provider, get_decrypted_token(account))
        try:
            url = await provider.get_download_url(remote_id)
        finally:
            with session_factory() as db:
                account = db.get(type(account), account_id)
                if account and persist_provider_token(account, provider):
                    db.commit()
        last_progress = 0.0

        async def cancelled():
            if context:
                return await context.cancellation_requested()
            with session_factory() as db:
                current = db.get(Task, task_id)
                return current is None or current.status == "cancel_requested"

        async def progress(count):
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress < 1 and count != expected_size:
                return
            last_progress = now
            with session_factory() as db:
                current = db.get(Task, task_id)
                if current and (not context or current.lock_token == context.lock_token):
                    total = f" / {expected_size:,}" if expected_size is not None else ""
                    current.message = f"下载中：{count:,}{total} 字节 → {relative}"
                    db.commit()

        settings = get_settings()
        count = await stream_download(
            url,
            target,
            expected_size,
            progress,
            cancelled,
            guard=guard,
            identity=remote_id,
            expected_hash=expected_hash,
            chunk_bytes=settings.download_chunk_bytes,
            rate_limit=settings.download_rate_limit_kib * 1024,
        )
        return TaskOutcome(status="success", summary=f"已下载 {count:,} 字节 → {target}")
    except DownloadDeferred as exc:
        return TaskOutcome(
            status="deferred",
            summary=f"下载已保存进度：{exc.count:,} 字节，等待继续",
            metrics={"continuation": True, "downloaded_bytes": exc.count},
            retry_after=timedelta(seconds=1),
        )
    except PikPakCredentialError as exc:
        return TaskOutcome(
            status="waiting_credential",
            error_code=exc.code,
            error_message=str(exc),
            blocked_reason="PikPak credential requires user action",
        )
    except PikPakRequestError as exc:
        return TaskOutcome(status="failed", error_code=exc.code, error_message=str(exc))
    except asyncio.CancelledError:
        return TaskOutcome(status="cancelled", summary="下载已取消，临时文件保留；重试可继续下载")
    except DownloadError as exc:
        return TaskOutcome(status="failed", error_code="DOWNLOAD_FAILED", error_message=str(exc))
    except OSError:
        return TaskOutcome(
            status="failed",
            error_code="DOWNLOAD_IO_ERROR",
            error_message="写入失败：检查剩余空间、目录权限及 NAS 挂载状态（需支持硬链接）",
        )
    except Exception:
        # Signed URLs and credentials must not appear in task errors.
        return TaskOutcome(
            status="failed",
            error_code="DOWNLOAD_REQUEST_FAILED",
            error_message="获取或下载文件失败，请检查账号和网络后重试",
        )


class DownloadTaskHandler:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def __call__(self, context):
        return await execute_download(context.task.task_id, self.session_factory, context=context)


async def process_pending_downloads():
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        from app.services.update_execution_gate import build_update_execution_gate

        if build_update_execution_gate().evaluate(db).blocked:
            return
        task = db.scalar(
            select(Task)
            .where(
                Task.type == "download",
                Task.status == "pending",
                or_(Task.next_attempt_at.is_(None), Task.next_attempt_at <= utcnow()),
            )
            .order_by(Task.id)
            .limit(1)
        )
        if task is None:
            return
        task.status = "running"
        task.started_at = utcnow()
        task.attempt_count += 1
        task.message = "正在获取下载地址"
        task_id = task.id
        db.commit()
    outcome = await execute_download(task_id, SessionLocal)
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task:
            # Legacy has no leased TaskRun; cooperative slices still release its slot.
            if task.status == "cancel_requested" and outcome.status == "deferred":
                outcome = TaskOutcome(status="cancelled", summary="下载已取消，临时文件保留")
            task.status = "pending" if outcome.status == "deferred" else outcome.status
            task.next_attempt_at = (
                utcnow() + timedelta(seconds=1) if outcome.status == "deferred" else None
            )
            task.message = outcome.summary or outcome.error_message
            task.error_code = outcome.error_code
            task.finished_at = None if outcome.status == "deferred" else utcnow()
            db.commit()
