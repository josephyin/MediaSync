from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1 import files as files_api
from app.api.v1 import subscriptions as subscriptions_api
from app.api.v1 import tasks as tasks_api
from app.core.config import Settings
from app.models import Base, CloudAccount, CloudFile, Subscription, Task, TaskRun

NOW = datetime(2026, 7, 24, 15, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "process-mode-api.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def mode_settings(mode: str) -> Settings:
    return Settings(
        _env_file=None,
        background_execution_mode=mode,
        manual_scan_cooldown_seconds=0,
    )


def seed_subscription_and_file(
    session: Session,
) -> tuple[Subscription, CloudFile]:
    account = CloudAccount(
        provider="aliyundrive",
        name="api-account",
        refresh_token="encrypted",
        status="active",
    )
    session.add(account)
    session.flush()
    subscription = Subscription(
        cloud_account_id=account.id,
        name="api-subscription",
        provider="aliyundrive",
        share_url="https://www.alipan.com/s/api",
        share_key="api",
        source_folder_id="root",
        target_path="/Media",
        schedule="interval:30m",
        enabled=True,
        status="active",
        initial_sync_mode="all",
        next_scan_at=NOW,
    )
    session.add(subscription)
    session.flush()
    file = CloudFile(
        subscription_id=subscription.id,
        remote_file_id="api-file",
        parent_remote_file_id="root",
        filename="api.mkv",
        relative_path="api.mkv",
        item_type="file",
        size=1024,
        fingerprint="api-fingerprint",
        status="failed",
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_error="failed",
    )
    session.add(file)
    session.flush()
    return subscription, file


def test_task_api_projects_latest_task_run_execution_information(
    sessions: sessionmaker[Session],
) -> None:
    finished_at = NOW.replace(minute=2)
    with sessions() as session, session.begin():
        subscription, file = seed_subscription_and_file(session)
        task = Task(
            account_id=subscription.cloud_account_id,
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            trigger_type="scheduled",
            status="success",
            retry_count=1,
            max_retries=3,
            completed_at=finished_at,
        )
        session.add(task)
        session.flush()
        session.add(
            TaskRun(
                task_id=task.id,
                run_number=2,
                worker_id="worker-task-api-test",
                lock_token="task-api-test-token",
                status="success",
                started_at=NOW,
                finished_at=finished_at,
                duration_ms=120_000,
                result_summary="已转存至 /Media/api.mkv",
            )
        )
        task_id = task.id

    with sessions() as session:
        page = tasks_api.list_tasks(
            db=session,
            _=None,  # type: ignore[arg-type]
            status="success",
        )
        detail = tasks_api.get_task(
            task_id=task_id,
            db=session,
            _=None,  # type: ignore[arg-type]
        )

    assert page.total == 1
    summary = page.items[0]
    for item in (summary, detail):
        assert item.started_at == NOW.replace(tzinfo=None)
        assert item.finished_at == finished_at.replace(tzinfo=None)
        assert item.next_attempt_at is None
        assert item.message == "已转存至 /Media/api.mkv"
        assert item.retry_count == 1
        assert item.max_retries == 3
        assert item.latest_run is not None
        assert item.latest_run.run_number == 2
        assert item.latest_run.duration_ms == 120_000


def test_delete_subscription_preserves_terminal_task_run_history(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriptions_api,
        "_remove_legacy_subscription_job",
        lambda _subscription_id: None,
    )
    with sessions() as session, session.begin():
        subscription, file = seed_subscription_and_file(session)
        task = Task(
            account_id=subscription.cloud_account_id,
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            status="success",
            completed_at=NOW,
        )
        session.add(task)
        session.flush()
        run = TaskRun(
            task_id=task.id,
            run_number=1,
            worker_id="worker-delete-test",
            lock_token="delete-test-token",
            status="success",
            started_at=NOW,
            finished_at=NOW,
            result_summary="转存成功",
        )
        session.add(run)
        session.flush()
        subscription_id = subscription.id
        file_id = file.id
        task_id = task.id
        run_id = run.id

    with sessions() as session:
        response = subscriptions_api.delete_subscription(
            subscription_id=subscription_id,
            db=session,
            _=None,  # type: ignore[arg-type]
        )

    assert response.message == "订阅已删除，Task 执行历史和云盘目标文件已保留"
    with sessions() as session:
        assert session.get(Subscription, subscription_id) is None
        assert session.get(CloudFile, file_id) is None
        preserved_task = session.get(Task, task_id)
        assert preserved_task is not None
        assert preserved_task.subscription_id is None
        assert preserved_task.file_id is None
        assert session.get(TaskRun, run_id) is not None


def test_delete_subscription_rejects_active_task(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriptions_api,
        "_remove_legacy_subscription_job",
        lambda _subscription_id: None,
    )
    with sessions() as session, session.begin():
        subscription, file = seed_subscription_and_file(session)
        task = Task(
            account_id=subscription.cloud_account_id,
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            status="retry",
        )
        session.add(task)
        session.flush()
        subscription_id = subscription.id
        file_id = file.id
        task_id = task.id

    with sessions() as session, pytest.raises(HTTPException) as exc_info:
        subscriptions_api.delete_subscription(
            subscription_id=subscription_id,
            db=session,
            _=None,  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "订阅仍有活动任务，请先停用订阅并等待任务结束后再删除"
    with sessions() as session:
        assert session.get(Subscription, subscription_id) is not None
        assert session.get(CloudFile, file_id) is not None
        assert session.get(Task, task_id) is not None


def test_process_manual_scan_enqueues_without_background_task(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriptions_api,
        "get_settings",
        lambda: mode_settings("process"),
    )
    with sessions() as session, session.begin():
        subscription, _file = seed_subscription_and_file(session)
        subscription_id = subscription.id

    background_tasks = BackgroundTasks()
    with sessions() as session:
        task = subscriptions_api.scan_subscription(
            subscription_id=subscription_id,
            background_tasks=background_tasks,
            db=session,
            _=None,  # type: ignore[arg-type]
            full=True,
        )

    with sessions() as session:
        persisted = session.get(Task, task.id)
        assert persisted is not None
        assert persisted.status == "pending"
        assert persisted.type == "scan"
        assert persisted.payload_version == 1
        assert persisted.payload == {"force_full": True}
        assert background_tasks.tasks == []


def test_legacy_manual_scan_keeps_background_execution_path(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriptions_api,
        "get_settings",
        lambda: mode_settings("legacy"),
    )
    with sessions() as session, session.begin():
        subscription, _file = seed_subscription_and_file(session)
        subscription_id = subscription.id

    background_tasks = BackgroundTasks()
    with sessions() as session:
        task = subscriptions_api.scan_subscription(
            subscription_id=subscription_id,
            background_tasks=background_tasks,
            db=session,
            _=None,  # type: ignore[arg-type]
            full=False,
        )

        assert task.status == "pending"
        assert len(background_tasks.tasks) == 1


def test_repeated_legacy_manual_scan_does_not_duplicate_background_task(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriptions_api,
        "get_settings",
        lambda: mode_settings("legacy"),
    )
    with sessions() as session, session.begin():
        subscription, _file = seed_subscription_and_file(session)
        subscription_id = subscription.id

    first_background_tasks = BackgroundTasks()
    second_background_tasks = BackgroundTasks()
    with sessions() as session:
        first = subscriptions_api.scan_subscription(
            subscription_id=subscription_id,
            background_tasks=first_background_tasks,
            db=session,
            _=None,  # type: ignore[arg-type]
            full=False,
        )
        second = subscriptions_api.scan_subscription(
            subscription_id=subscription_id,
            background_tasks=second_background_tasks,
            db=session,
            _=None,  # type: ignore[arg-type]
            full=True,
        )

        assert second.id == first.id
        assert len(first_background_tasks.tasks) == 1
        assert second_background_tasks.tasks == []


def test_repeated_process_manual_scan_returns_active_task(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscriptions_api,
        "get_settings",
        lambda: mode_settings("process"),
    )
    with sessions() as session, session.begin():
        subscription, _file = seed_subscription_and_file(session)
        subscription_id = subscription.id

    with sessions() as session:
        first = subscriptions_api.scan_subscription(
            subscription_id=subscription_id,
            background_tasks=BackgroundTasks(),
            db=session,
            _=None,  # type: ignore[arg-type]
            full=False,
        )
        second = subscriptions_api.scan_subscription(
            subscription_id=subscription_id,
            background_tasks=BackgroundTasks(),
            db=session,
            _=None,  # type: ignore[arg-type]
            full=True,
        )

        assert second.id == first.id
        assert second.payload == {"force_full": False}
        assert (
            len(
                list(
                    session.scalars(
                        select(Task).where(
                            Task.subscription_id == subscription_id,
                            Task.type == "scan",
                        )
                    )
                )
            )
            == 1
        )


def test_process_mode_subscription_hooks_do_not_touch_apscheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []
    subscription = Subscription(
        id=42,
        cloud_account_id=1,
        name="test",
        provider="aliyundrive",
        share_url="share",
        target_path="/Media",
        schedule="interval:30m",
        enabled=True,
    )
    monkeypatch.setattr(
        subscriptions_api,
        "get_settings",
        lambda: mode_settings("process"),
    )
    monkeypatch.setattr(
        subscriptions_api,
        "upsert_subscription_job",
        lambda item: calls.append(("upsert", item.id)),
    )
    monkeypatch.setattr(
        subscriptions_api,
        "remove_subscription_job",
        lambda item_id: calls.append(("remove", item_id)),
    )

    subscriptions_api._upsert_legacy_subscription_job(subscription)
    subscriptions_api._remove_legacy_subscription_job(subscription.id)

    assert calls == []


def test_file_retry_api_creates_successor_task(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription, file = seed_subscription_and_file(session)
        predecessor = Task(
            account_id=subscription.cloud_account_id,
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            trigger_type="scheduled",
            status="failed",
            payload_version=1,
            payload={},
            idempotency_key="original-transfer",
        )
        session.add(predecessor)
        session.flush()
        predecessor_id = predecessor.id
        file_id = file.id

    with sessions() as session:
        successor = files_api.retry_file(
            file_id=file_id,
            db=session,
            _=None,  # type: ignore[arg-type]
        )

    with sessions() as session:
        predecessor = session.get(Task, predecessor_id)
        successor = session.get(Task, successor.id)

        assert predecessor is not None
        assert predecessor.status == "failed"
        assert predecessor.idempotency_key == "original-transfer"
        assert successor is not None
        assert successor.id != predecessor.id
        assert successor.status == "pending"
        assert successor.idempotency_key == (f"transfer-retry:{file_id}:{predecessor_id}")
