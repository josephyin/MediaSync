import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.models import CloudFile, Subscription, Task
from app.repositories import TaskRepository
from app.services.download_service import DownloadError, destination_path
from app.services.download_settings import (
    default_download_root,
    save_download_root,
    validate_directory,
)

router = APIRouter(prefix="/downloads", tags=["downloads"])


def _create_download(db, file, root, relative, *, predecessor_id=0):
    stat = root.stat()
    location = hashlib.sha256(f"{root}\0{relative}".encode()).hexdigest()
    return TaskRepository(db).create_task(
        Task(
            type="download",
            trigger_type="retry" if predecessor_id else "manual",
            status="pending",
            account_id=file.subscription.cloud_account_id,
            subscription_id=file.subscription_id,
            file_id=file.id,
            payload={
                "relative_path": relative,
                "root": str(root),
                "root_identity": [stat.st_dev, stat.st_ino],
            },
            idempotency_key=f"download:{file.id}:{location}:{predecessor_id}",
            message=f"等待下载 → {relative}",
        )
    )


class DownloadRequest(BaseModel):
    file_ids: list[int] = Field(min_length=1, max_length=10000)
    root: str | None = Field(default=None, max_length=4096)
    directory: str = Field(default="", max_length=500)


@router.get("/config")
def download_config(_: AdminUser):
    try:
        root = default_download_root()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"root": str(root), "available": root.is_dir(), "providers": ["pikpak"]}


class DownloadLocation(BaseModel):
    root: str = Field(min_length=1, max_length=4096)


@router.put("/config")
def set_download_config(payload: DownloadLocation, _: AdminUser):
    try:
        root = validate_directory(payload.root)
        save_download_root(root)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError:
        raise HTTPException(400, "无法保存下载位置，请检查配置目录权限") from None
    return {"root": str(root), "available": True, "providers": ["pikpak"]}


@router.get("/directories")
def browse_directories(_: AdminUser, path: str | None = None):
    try:
        current = Path(path).expanduser() if path else default_download_root()
        if not current.is_absolute():
            raise ValueError("请输入绝对路径")
        current = current.resolve(strict=True)
        if not current.is_dir():
            raise ValueError("目录不存在")
        children = sorted(
            (item for item in current.iterdir() if not item.is_symlink() and item.is_dir()),
            key=lambda item: item.name.casefold(),
        )
        return {
            "path": str(current),
            "parent": str(current.parent),
            "directories": [{"name": item.name, "path": str(item)} for item in children[:1000]],
            "truncated": len(children) > 1000,
        }
    except (ValueError, OSError) as exc:
        raise HTTPException(400, "无法浏览此目录，请检查路径、权限及 NAS 挂载状态") from exc


@router.get("/selection")
def download_selection(
    db: DbSession,
    _: AdminUser,
    subscription_id: int | None = None,
    query: str | None = None,
    status: str | None = None,
):
    conditions = [
        CloudFile.status == "saved",
        CloudFile.item_type == "file",
        CloudFile.target_file_id.is_not(None),
        CloudFile.target_file_id != "",
        Subscription.provider == "pikpak",
    ]
    if subscription_id is not None:
        conditions.append(CloudFile.subscription_id == subscription_id)
    if query:
        conditions.append(
            or_(CloudFile.filename.contains(query), CloudFile.relative_path.contains(query))
        )
    if status:
        conditions.append(CloudFile.status == status)
    ids = list(
        db.scalars(
            select(CloudFile.id)
            .join(Subscription)
            .where(*conditions)
            .order_by(CloudFile.id)
            .limit(10001)
        )
    )
    if len(ids) > 10000:
        raise HTTPException(409, "可下载文件超过 10,000 个，请缩小订阅或搜索范围后再选择")
    return {"file_ids": ids, "count": len(ids)}


@router.post("", status_code=202)
def enqueue_downloads(payload: DownloadRequest, db: DbSession, _: AdminUser):
    try:
        root = validate_directory(payload.root or str(default_download_root()))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    selected_ids = list(dict.fromkeys(payload.file_ids))
    files_by_id = {}
    active_by_file = {}
    for offset in range(0, len(selected_ids), 500):
        batch = selected_ids[offset : offset + 500]
        for file in db.scalars(
            select(CloudFile)
            .options(selectinload(CloudFile.subscription))
            .where(CloudFile.id.in_(batch))
        ):
            files_by_id[file.id] = file
        for task in db.scalars(
            select(Task).where(
                Task.file_id.in_(batch),
                Task.type == "download",
                Task.status.in_(
                    ["pending", "running", "retry", "cancel_requested", "waiting_credential"]
                ),
            )
        ):
            active_by_file.setdefault(task.file_id, []).append(task)
    prepared = []
    for file_id in selected_ids:
        file = files_by_id.get(file_id)
        if file is None:
            raise HTTPException(404, "文件不存在")
        if (
            file.subscription.provider != "pikpak"
            or file.status != "saved"
            or not file.target_file_id
            or file.item_type != "file"
        ):
            raise HTTPException(400, "请选择已转存的 PikPak 文件")
        relative = "/".join(
            filter(None, [payload.directory.strip(), file.relative_path or file.filename])
        )
        try:
            destination_path(root, relative)
        except DownloadError as exc:
            raise HTTPException(400, str(exc)) from None
        prepared.append((file, relative))
    ids = []
    for file, relative in prepared:
        existing = active_by_file.get(file.id, [])
        task = next(
            (
                x
                for x in existing
                if x.payload.get("relative_path") == relative
                and x.payload.get(
                    "root", str(Path(get_settings().download_root).expanduser().absolute())
                )
                == str(root)
            ),
            None,
        )
        if task is None:
            previous = db.scalars(
                select(Task)
                .where(
                    Task.file_id == file.id,
                    Task.type == "download",
                )
                .order_by(Task.id.desc())
            )
            predecessor = next(
                (
                    x
                    for x in previous
                    if x.payload.get("root") == str(root)
                    and x.payload.get("relative_path") == relative
                ),
                None,
            )
            task = _create_download(
                db, file, root, relative, predecessor_id=predecessor.id if predecessor else 0
            )
        ids.append(task.id)
    db.commit()
    return {"task_ids": ids}


@router.post("/{task_id}/retry", status_code=202)
def retry_download(task_id: int, db: DbSession, _: AdminUser):
    task = db.get(Task, task_id)
    if not task or task.type != "download":
        raise HTTPException(404, "下载任务不存在")
    if task.status == "waiting_credential":
        TaskRepository(db).transition(
            task.id, expected_status="waiting_credential", target_status="pending"
        )
        db.commit()
        return {"task_id": task.id}
    if task.status not in ("failed", "cancelled"):
        raise HTTPException(409, "只能重试失败或已取消的下载")
    try:
        root = validate_directory(str(task.payload["root"]))
    except (ValueError, KeyError):
        raise HTTPException(400, "下载目录不可用，请检查 NAS 挂载后重试") from None
    active = db.scalars(
        select(Task).where(
            Task.file_id == task.file_id,
            Task.type == "download",
            Task.status.in_(
                ["pending", "running", "retry", "waiting_credential", "cancel_requested"]
            ),
        )
    )
    successor = next(
        (
            x
            for x in active
            if x.payload.get("root") == str(root)
            and x.payload.get("relative_path") == task.payload["relative_path"]
        ),
        None,
    )
    if successor is None:
        successor = _create_download(
            db, task.file, root, task.payload["relative_path"], predecessor_id=task.id
        )
    db.commit()
    return {"task_id": successor.id}


@router.post("/{task_id}/cancel", status_code=202)
def cancel_download(task_id: int, db: DbSession, _: AdminUser):
    task = db.get(Task, task_id)
    if not task or task.type != "download":
        raise HTTPException(404, "下载任务不存在")
    if task.status == "running":
        TaskRepository(db).transition(
            task.id, expected_status="running", target_status="cancel_requested"
        )
    elif task.status in ("pending", "retry", "waiting_credential"):
        TaskRepository(db).transition(
            task.id, expected_status=task.status, target_status="cancelled"
        )
    else:
        raise HTTPException(409, "当前下载不能取消")
    db.commit()
    return {"task_id": task.id}
