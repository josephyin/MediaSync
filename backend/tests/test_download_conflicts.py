from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api.v1 import downloads
from app.models import Base, CloudAccount, CloudFile, Subscription, Task
from app.models.base import utcnow
from app.repositories import TaskRepository


@pytest.fixture
def environment(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'conflicts.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    ids = []
    with sessions.begin() as db:
        account = CloudAccount(provider="pikpak", name="test", refresh_token="encrypted")
        db.add(account)
        db.flush()
        for i in range(2):
            sub = Subscription(
                cloud_account_id=account.id,
                name=f"subscription-{i}",
                provider="pikpak",
                share_url=f"https://mypikpak.com/s/test-{i}",
                target_path="/",
            )
            db.add(sub)
            db.flush()
            file = CloudFile(
                subscription_id=sub.id,
                remote_file_id=f"source-{i}",
                filename="movie",
                relative_path="season/movie" if i == 0 else "season//movie",
                fingerprint=f"hash-{i}",
                status="saved",
                target_file_id=f"target-{i}",
                size=5,
                first_seen_at=utcnow(),
                last_seen_at=utcnow(),
            )
            db.add(file)
            db.flush()
            ids.append(file.id)
    yield sessions, ids, tmp_path
    engine.dispose()


def enqueue(db, ids, root, **kwargs):
    return downloads.enqueue_downloads(
        downloads.DownloadRequest(
            file_ids=ids,
            root=str(root),
            **kwargs,
        ),
        db,
        "admin",
    )


def test_colliding_batch_is_rejected_before_any_task_is_created(environment):
    sessions, ids, root = environment
    with sessions() as db:
        with pytest.raises(HTTPException) as caught:
            enqueue(db, ids, root)
        assert caught.value.status_code == 409
        assert db.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.parametrize("status", downloads.ACTIVE_DOWNLOAD_STATUSES)
def test_active_destination_is_reserved_across_subscriptions(environment, status):
    sessions, ids, root = environment
    with sessions() as db:
        first = enqueue(db, [ids[0]], root)
        task = db.get(Task, first["task_ids"][0])
        task.status = status
        db.commit()
        assert enqueue(db, [ids[0]], root) == first
        with pytest.raises(HTTPException) as caught:
            enqueue(db, [ids[1]], root)
        assert caught.value.status_code == 409
        assert db.scalar(select(func.count()).select_from(Task)) == 1
        assert db.get(Task, task.id).status == status


def test_repeat_retry_returns_completed_successor_without_rewriting_history(environment):
    sessions, ids, root = environment
    with sessions() as db:
        original_id = enqueue(db, [ids[0]], root)["task_ids"][0]
        downloads.cancel_download(original_id, db, "admin")
        next_id = downloads.retry_download(original_id, db, "admin")["task_id"]
        db.get(Task, next_id).status = "success"
        db.commit()
        assert downloads.retry_download(original_id, db, "admin")["task_id"] == next_id
        assert db.get(Task, original_id).status == "cancelled"
        assert db.get(Task, next_id).status == "success"


def test_retry_cannot_take_another_files_reserved_destination(environment):
    sessions, ids, root = environment
    with sessions() as db:
        original = enqueue(db, [ids[0]], root)["task_ids"][0]
        downloads.cancel_download(original, db, "admin")
        second = enqueue(db, [ids[1]], root)["task_ids"][0]
        with pytest.raises(HTTPException) as caught:
            downloads.retry_download(original, db, "admin")
        assert caught.value.status_code == 409
        assert db.get(Task, second).status == "pending"


def test_savepoint_does_not_commit_partial_batch(environment):
    sessions, ids, root = environment
    with sessions() as db:
        file = db.get(CloudFile, ids[0])
        downloads._create_download(db, file, root, "movie")
        db.rollback()
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 0


@pytest.mark.parametrize("same_file", [False, True])
def test_simultaneous_destination_claims_are_atomic(environment, monkeypatch, same_file):
    sessions, ids, root = environment
    barrier = Barrier(2)
    original_create = TaskRepository.create_task

    def collide(repository, task):
        barrier.wait(timeout=5)
        return original_create(repository, task)

    monkeypatch.setattr(TaskRepository, "create_task", collide)

    def request(file_id):
        with sessions() as db:
            try:
                task = downloads._create_download(db, db.get(CloudFile, file_id), root, "movie")
                db.commit()
                return (202, task.id)
            except HTTPException as exc:
                db.rollback()
                return (exc.status_code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(request, ids[0])
        b = pool.submit(request, ids[0] if same_file else ids[1])
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sorted(status for status, _id in results) == ([202, 202] if same_file else [202, 409])
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Task)) == 1
