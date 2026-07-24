from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    Base,
    CloudAccount,
    CloudFile,
    Subscription,
    Task,
    TaskRun,
)
from app.services.task_enqueue_service import (
    enqueue_manual_scan,
    enqueue_transfer_retry,
)

NOW = datetime(2026, 7, 24, 14, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "task-enqueue-service.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def seed_subscription_and_file(
    session: Session,
) -> tuple[Subscription, CloudFile]:
    account = CloudAccount(
        provider="aliyundrive",
        name="enqueue-account",
        refresh_token="encrypted",
        status="active",
    )
    session.add(account)
    session.flush()
    subscription = Subscription(
        cloud_account_id=account.id,
        name="enqueue-subscription",
        provider="aliyundrive",
        share_url="https://www.alipan.com/s/enqueue",
        share_key="enqueue",
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
        remote_file_id="remote-1",
        parent_remote_file_id="root",
        filename="movie.mkv",
        relative_path="movie.mkv",
        item_type="file",
        size=1024,
        fingerprint="fingerprint-1",
        status="failed",
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_error="previous failure",
    )
    session.add(file)
    session.flush()
    return subscription, file


def test_manual_scan_enqueues_versioned_pending_task(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription, _file = seed_subscription_and_file(session)
        result = enqueue_manual_scan(
            session,
            subscription,
            force_full=True,
        )
        task = result.task
        task_id = task.id
        subscription_id = subscription.id
        account_id = subscription.cloud_account_id

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.account_id == account_id
        assert task.subscription_id == subscription_id
        assert task.type == "scan"
        assert task.trigger_type == "manual"
        assert task.status == "pending"
        assert task.payload_version == 1
        assert task.payload == {"force_full": True}
        assert result.created is True


@pytest.mark.parametrize(
    "active_status",
    ["pending", "running", "retry", "waiting_credential", "cancel_requested"],
)
def test_manual_scan_returns_existing_non_terminal_task(
    sessions: sessionmaker[Session],
    active_status: str,
) -> None:
    with sessions() as session, session.begin():
        subscription, _file = seed_subscription_and_file(session)
        existing = Task(
            subscription_id=subscription.id,
            type="scan",
            trigger_type="scheduled",
            status=active_status,
            payload_version=1,
            payload={"force_full": False},
        )
        session.add(existing)
        session.flush()

        result = enqueue_manual_scan(
            session,
            subscription,
            force_full=True,
        )
        returned = result.task

        assert returned.id == existing.id
        assert returned.payload == {"force_full": False}
        assert result.created is False
        assert (
            len(
                list(
                    session.scalars(
                        select(Task).where(
                            Task.subscription_id == subscription.id,
                            Task.type == "scan",
                        )
                    )
                )
            )
            == 1
        )


def test_transfer_retry_creates_successor_without_mutating_terminal_history(
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
            idempotency_key="transfer:original",
            retry_count=3,
            last_error_code="RETRY_EXHAUSTED",
            completed_at=NOW,
        )
        session.add(predecessor)
        session.flush()
        predecessor_run = TaskRun(
            task_id=predecessor.id,
            run_number=1,
            worker_id="worker-old",
            lock_token="old-token",
            status="failed",
            started_at=NOW,
            finished_at=NOW,
            error_code="RETRY_EXHAUSTED",
            metrics={},
        )
        session.add(predecessor_run)
        session.flush()
        predecessor_id = predecessor.id
        predecessor_run_id = predecessor_run.id
        file_id = file.id

        successor = enqueue_transfer_retry(session, file)
        successor_id = successor.id

    with sessions() as session:
        predecessor = session.get(Task, predecessor_id)
        predecessor_run = session.get(TaskRun, predecessor_run_id)
        successor = session.get(Task, successor_id)
        file = session.get(CloudFile, file_id)

        assert predecessor is not None
        assert predecessor.status == "failed"
        assert predecessor.retry_count == 3
        assert predecessor.last_error_code == "RETRY_EXHAUSTED"
        assert predecessor.idempotency_key == "transfer:original"
        assert predecessor_run is not None
        assert predecessor_run.status == "failed"
        assert predecessor_run.error_code == "RETRY_EXHAUSTED"

        assert successor is not None
        assert successor.id != predecessor.id
        assert successor.status == "pending"
        assert successor.type == "transfer"
        assert successor.trigger_type == "retry"
        assert successor.payload_version == 1
        assert successor.payload == {}
        assert successor.idempotency_key == (f"transfer-retry:{file_id}:{predecessor_id}")
        assert file is not None
        assert file.status == "pending"
        assert file.last_error is None


def test_repeated_transfer_retry_returns_active_successor(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        _subscription, file = seed_subscription_and_file(session)
        first = enqueue_transfer_retry(session, file)
        second = enqueue_transfer_retry(session, file)

        assert second.id == first.id
        assert (
            len(
                list(
                    session.scalars(
                        select(Task).where(
                            Task.file_id == file.id,
                            Task.type == "transfer",
                        )
                    )
                )
            )
            == 1
        )


def test_active_transfer_is_returned_without_rewriting_file_state(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription, file = seed_subscription_and_file(session)
        active = Task(
            account_id=subscription.cloud_account_id,
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            trigger_type="scheduled",
            status="running",
            payload_version=1,
            payload={},
        )
        session.add(active)
        session.flush()

        returned = enqueue_transfer_retry(session, file)

        assert returned.id == active.id
        assert file.status == "failed"
        assert file.last_error == "previous failure"
