from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.models import CloudFile, Task
from app.schemas.common import Page
from app.schemas.file import CloudFileRead
from app.schemas.task import TaskRead
from app.services.task_enqueue_service import enqueue_transfer_retry

router = APIRouter(prefix="/files", tags=["files"])


@router.get("", response_model=Page[CloudFileRead])
def list_files(
    db: DbSession,
    _: AdminUser,
    page: int = 1,
    page_size: int = 20,
    subscription_id: int | None = None,
    status: str | None = None,
    query: str | None = None,
) -> Page[CloudFileRead]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    conditions = []
    if subscription_id is not None:
        conditions.append(CloudFile.subscription_id == subscription_id)
    if status:
        conditions.append(CloudFile.status == status)
    if query:
        conditions.append(CloudFile.filename.contains(query))
    total = db.scalar(select(func.count()).select_from(CloudFile).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(CloudFile)
            .where(*conditions)
            .order_by(CloudFile.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page(items=items, page=page, page_size=page_size, total=total)


@router.get("/{file_id}", response_model=CloudFileRead)
def get_file(file_id: int, db: DbSession, _: AdminUser) -> CloudFile:
    file = db.get(CloudFile, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    return file


@router.post("/{file_id}/retry", response_model=TaskRead, status_code=202)
def retry_file(file_id: int, db: DbSession, _: AdminUser) -> Task:
    file = db.get(CloudFile, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="File not found")
    task = enqueue_transfer_retry(db, file)
    db.commit()
    db.refresh(task)
    return task
