from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.models import Task
from app.schemas.common import Page
from app.schemas.task import TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("", response_model=Page[TaskRead])
def list_tasks(
    db: DbSession,
    _: AdminUser,
    page: int = 1,
    page_size: int = 20,
    subscription_id: int | None = None,
    type: str | None = None,
    status: str | None = None,
) -> Page[TaskRead]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    conditions = []
    if subscription_id is not None:
        conditions.append(Task.subscription_id == subscription_id)
    if type:
        conditions.append(Task.type == type)
    if status:
        conditions.append(Task.status == status)
    total = db.scalar(select(func.count()).select_from(Task).where(*conditions)) or 0
    items = list(
        db.scalars(
            select(Task)
            .where(*conditions)
            .order_by(Task.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page(items=items, page=page, page_size=page_size, total=total)


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: int, db: DbSession, _: AdminUser) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task
