from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.v1 import files as files_api
from app.api.v1 import subscriptions as subscriptions_api
from app.core.config import Settings
from app.models import Base, CloudAccount, CloudFile, Subscription, Task

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
        assert len(
            list(
                session.scalars(
                    select(Task).where(
                        Task.subscription_id == subscription_id,
                        Task.type == "scan",
                    )
                )
            )
        ) == 1


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
        assert successor.idempotency_key == (
            f"transfer-retry:{file_id}:{predecessor_id}"
        )
