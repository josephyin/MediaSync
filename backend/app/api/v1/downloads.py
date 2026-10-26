import hashlib
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.api.deps import AdminUser, DbSession
from app.models import CloudFile, Subscription, Task
from app.repositories import TaskRepository
from app.services.download_service import DownloadError, destination_path
from app.services.download_settings import (
    default_download_root,
    save_download_root,
    validate_directory,
)

router = APIRouter(prefix="/downloads", tags=["downloads"])
ACTIVE_DOWNLOAD_STATUSES = ("pending", "running", "retry", "cancel_requested", "waiting_credential")


def _destination_tasks(db, root, relatives):
    active, latest = {}, {}
    for offset in range(0, len(relatives), 200):
        scope = [
            Task.type == "download",
            Task.payload["root"].as_string() == str(root),
            Task.payload["relative_path"].as_string().in_(relatives[offset : offset + 200]),
        ]
        # Keep history lookup bounded to the latest task plus any active tasks.
        latest_ids = (
            select(func.max(Task.id))
            .where(*scope)
            .group_by(Task.payload["relative_path"].as_string())
        )
        rows = db.scalars(
            select(Task)
            .where(
                *scope,
                or_(Task.status.in_(ACTIVE_DOWNLOAD_STATUSES), Task.id.in_(latest_ids)),
            )
            .order_by(Task.id.desc())
        )
        for task in rows:
            relative = task.payload["relative_path"]
            latest.setdefault(relative, task)
            if task.status in ACTIVE_DOWNLOAD_STATUSES:
                active.setdefault(relative, []).append(task)
    return active, latest


def _check_destination(tasks, file_id):
    if any(task.file_id != file_id for task in tasks):
        raise HTTPException(409, "其他文件正在使用相同下载路径，请选择不同子目录")
    return tasks[0] if tasks else None


def _create_download(db, file, root, relative, *, predecessor_id=0):
    stat = root.stat()
    account_id = file.subscription.cloud_account_id
    location = hashlib.sha256(f"{root}\0{relative}".encode()).hexdigest()
    # A destination-scoped key also arbitrates simultaneous requests for different
    # files at the same path. Savepoints must belong to a real outer transaction.
    key = f"download:{location}:{predecessor_id}"
    existing = db.scalar(select(Task).where(Task.idempotency_key == key))
    if existing is not None:
        return _check_destination([existing], file.id)
    connection = db.connection()
    if (
        connection.dialect.name == "sqlite"
        and not connection.connection.driver_connection.in_transaction
    ):
        connection.exec_driver_sql("BEGIN")
    try:
        with db.begin_nested():
            return TaskRepository(db).create_task(
                Task(
                    type="download",
                    trigger_type="retry" if predecessor_id else "manual",
                    status="pending",
                    account_id=account_id,
                    subscription_id=file.subscription_id,
                    file_id=file.id,
                    payload={
                        "relative_path": relative,
                        "root": str(root),
                        "root_identity": [stat.st_dev, stat.st_ino],
                    },
                    idempotency_key=key,
                    message=f"等待下载 → {relative}",
                )
            )
    except IntegrityError:
        existing = db.scalar(select(Task).where(Task.idempotency_key == key))
        if existing is None:
            raise
        return _check_destination([existing], file.id)


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
    for offset in range(0, len(selected_ids), 500):
        batch = selected_ids[offset : offset + 500]
        for file in db.scalars(
            select(CloudFile)
            .options(selectinload(CloudFile.subscription))
            .where(CloudFile.id.in_(batch))
        ):
            files_by_id[file.id] = file
    prepared = []
    destinations = set()
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
            relative = destination_path(root, relative).relative_to(root).as_posix()
        except DownloadError as exc:
            raise HTTPException(400, str(exc)) from None
        if relative in destinations:
            raise HTTPException(409, "所选文件包含相同下载路径，请按订阅选择不同子目录")
        destinations.add(relative)
        prepared.append((file, relative))
    active, latest = _destination_tasks(db, root, [relative for _file, relative in prepared])
    for file, relative in prepared:
        _check_destination(active.get(relative, []), file.id)
    ids = []
    for file, relative in prepared:
        task = _check_destination(active.get(relative, []), file.id)
        if task is None:
            predecessor = latest.get(relative)
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
    active, _latest = _destination_tasks(db, root, [task.payload["relative_path"]])
    successor = _check_destination(active.get(task.payload["relative_path"], []), task.file_id)
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
