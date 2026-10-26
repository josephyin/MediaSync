"""Local downloads share the task queue, but never change cloud transfer state."""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select

from app.core.config import get_settings
from app.models import Task
from app.models.base import utcnow
from app.providers import get_provider
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.task_engine.handlers import TaskOutcome


class DownloadError(ValueError):
    pass


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


def validate_download_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not (host == "mypikpak.com" or host.endswith(".mypikpak.com"))
    ):
        raise DownloadError("PikPak 返回了不受支持的下载地址")


async def stream_download(url, target, expected_size, progress, cancelled, *, client=None):
    """Write a private partial file; publish only after a complete size check."""
    validate_download_url(url)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name("." + target.name + ".mediasync.part")
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(partial, flags, 0o600)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=20), trust_env=False)
    try:
        with os.fdopen(fd, "w+b") as output:
            try:
                fcntl.flock(output, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DownloadError("同一目标文件已有下载任务") from None
            if target.exists() or target.is_symlink():
                raise DownloadError("目标文件已存在，请换一个目录；不会覆盖现有文件")
            output.truncate(0)
            written = 0
            for redirects in range(6):
                validate_download_url(url)
                async with client.stream("GET", url, follow_redirects=False) as response:
                    if response.is_redirect:
                        if redirects == 5 or "location" not in response.headers:
                            raise DownloadError("下载重定向次数过多")
                        url = str(response.url.join(response.headers["location"]))
                        continue
                    if response.status_code != 200:
                        raise DownloadError(
                            f"下载请求失败（HTTP {response.status_code}），可重试获取新链接"
                        )
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        if await cancelled():
                            raise asyncio.CancelledError
                        written += len(chunk)
                        if expected_size is not None and written > expected_size:
                            raise DownloadError("下载内容超出文件记录大小")
                        await asyncio.to_thread(output.write, chunk)
                        await progress(written)
                    break
            if expected_size is not None and written != expected_size:
                raise DownloadError("文件大小校验失败，未发布不完整文件")
            if await cancelled():
                raise asyncio.CancelledError
            await asyncio.to_thread(output.flush)
            await asyncio.to_thread(os.fsync, output.fileno())
            # Hard-link publication is atomic and never replaces an existing file.
            # Unsupported NAS filesystems fail explicitly instead of overwriting.
            await asyncio.to_thread(os.link, partial, target)
            partial.unlink()
            return written
    finally:
        if owned:
            await client.aclose()


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
            target = destination_path(
                Path(str(task.payload.get("root") or get_settings().download_root)).expanduser(),
                relative,
            )
            expected_size = file.size
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

        count = await stream_download(url, target, expected_size, progress, cancelled)
        return TaskOutcome(status="success", summary=f"已下载 {count:,} 字节 → {target}")
    except asyncio.CancelledError:
        return TaskOutcome(status="cancelled", summary="下载已取消，临时文件保留；重试会重新下载")
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
            task.status = outcome.status
            task.message = outcome.summary or outcome.error_message
            task.error_code = outcome.error_code
            task.finished_at = utcnow()
            db.commit()
