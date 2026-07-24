from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base, CloudAccount, Subscription, Task
from app.scheduler.enqueue import enqueue_due_scan_tasks

NOW = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "scheduler-enqueue.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def seed_subscription(
    session: Session,
    *,
    name: str = "test",
    due_at: datetime = NOW,
    enabled: bool = True,
    schedule: str = "interval:30m",
) -> Subscription:
    account = CloudAccount(
        provider="aliyundrive",
        name=f"{name}-account",
        refresh_token="encrypted",
        status="active",
    )
    session.add(account)
    session.flush()
    subscription = Subscription(
        cloud_account_id=account.id,
        name=name,
        provider="aliyundrive",
        share_url=f"https://www.alipan.com/s/{name}",
        share_key=name,
        source_folder_id="root",
        target_path="/Media",
        schedule=schedule,
        enabled=enabled,
        status="active" if enabled else "disabled",
        initial_sync_mode="all",
        next_scan_at=due_at,
    )
    session.add(subscription)
    session.flush()
    return subscription


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def test_due_subscription_enqueues_scan_v1_and_advances_schedule(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription = seed_subscription(session, due_at=NOW - timedelta(minutes=5))
        subscription_id = subscription.id
        account_id = subscription.cloud_account_id
        result = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session:
        task = session.scalar(select(Task))
        subscription = session.get(Subscription, subscription_id)

        assert result.inspected_count == 1
        assert result.enqueued_count == 1
        assert result.skipped_active_count == 0
        assert result.task_ids == (task.id,)
        assert task.account_id == account_id
        assert task.subscription_id == subscription_id
        assert task.type == "scan"
        assert task.trigger_type == "scheduled"
        assert task.status == "pending"
        assert task.payload_version == 1
        assert task.payload == {"force_full": False}
        assert task.idempotency_key == (
            f"scan:{subscription_id}:scheduled:2026-07-24T09:55:00.000000+00:00"
        )
        assert subscription is not None
        assert as_utc(subscription.next_scan_at) == NOW + timedelta(minutes=30)


def test_not_due_and_disabled_subscriptions_are_ignored(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        seed_subscription(
            session,
            name="future",
            due_at=NOW + timedelta(minutes=1),
        )
        seed_subscription(
            session,
            name="disabled",
            due_at=NOW - timedelta(hours=1),
            enabled=False,
        )
        result = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session:
        assert result.inspected_count == 0
        assert result.enqueued_count == 0
        assert list(session.scalars(select(Task))) == []


@pytest.mark.parametrize(
    "active_status",
    ["pending", "running", "retry", "waiting_credential", "cancel_requested"],
)
def test_active_scan_suppresses_duplicate_and_advances_schedule(
    sessions: sessionmaker[Session],
    active_status: str,
) -> None:
    with sessions() as session, session.begin():
        subscription = seed_subscription(session)
        subscription_id = subscription.id
        session.add(
            Task(
                subscription_id=subscription.id,
                type="scan",
                trigger_type="manual",
                status=active_status,
                payload_version=1,
                payload={"force_full": True},
            )
        )
        session.flush()
        result = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session:
        tasks = list(session.scalars(select(Task)))
        subscription = session.get(Subscription, subscription_id)

        assert result.inspected_count == 1
        assert result.enqueued_count == 0
        assert result.skipped_active_count == 1
        assert len(tasks) == 1
        assert tasks[0].trigger_type == "manual"
        assert subscription is not None
        assert as_utc(subscription.next_scan_at) == NOW + timedelta(minutes=30)


def test_terminal_scan_does_not_suppress_new_scheduled_task(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription = seed_subscription(session)
        session.add(
            Task(
                subscription_id=subscription.id,
                type="scan",
                status="success",
                payload_version=1,
                payload={},
            )
        )
        result = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session:
        assert result.enqueued_count == 1
        assert len(list(session.scalars(select(Task)))) == 2


def test_repeated_scheduling_boundary_does_not_duplicate_task(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        seed_subscription(session)
        first = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session, session.begin():
        second = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session:
        assert first.enqueued_count == 1
        assert second.inspected_count == 0
        assert len(list(session.scalars(select(Task)))) == 1


def test_task_and_next_scan_at_roll_back_together(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription = seed_subscription(session)
        subscription_id = subscription.id

    with pytest.raises(RuntimeError, match="force rollback"):
        with sessions() as session, session.begin():
            enqueue_due_scan_tasks(session, scheduled_at=NOW)
            raise RuntimeError("force rollback")

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        assert subscription is not None
        assert as_utc(subscription.next_scan_at) == NOW
        assert list(session.scalars(select(Task))) == []


def test_overdue_intervals_coalesce_from_current_scheduling_time(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription = seed_subscription(
            session,
            due_at=NOW - timedelta(days=3),
            schedule="interval:2h",
        )
        subscription_id = subscription.id
        result = enqueue_due_scan_tasks(session, scheduled_at=NOW)

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        assert result.enqueued_count == 1
        assert subscription is not None
        assert as_utc(subscription.next_scan_at) == NOW + timedelta(hours=2)


def test_batch_limit_uses_oldest_due_subscription_first(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        newest = seed_subscription(
            session,
            name="newest",
            due_at=NOW - timedelta(minutes=1),
        )
        oldest = seed_subscription(
            session,
            name="oldest",
            due_at=NOW - timedelta(hours=2),
        )
        oldest_id = oldest.id
        newest_id = newest.id
        result = enqueue_due_scan_tasks(session, scheduled_at=NOW, limit=1)

    with sessions() as session:
        task = session.scalar(select(Task))
        newest = session.get(Subscription, newest_id)

        assert result.inspected_count == 1
        assert task.subscription_id == oldest_id
        assert newest is not None
        assert as_utc(newest.next_scan_at) == NOW - timedelta(minutes=1)


@pytest.mark.parametrize("limit", [0, 1001])
def test_batch_limit_is_bounded(
    sessions: sessionmaker[Session],
    limit: int,
) -> None:
    with sessions() as session:
        with pytest.raises(ValueError, match="limit must be between"):
            enqueue_due_scan_tasks(session, scheduled_at=NOW, limit=limit)
