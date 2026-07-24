import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import Base, CloudAccount, Subscription, Task
from app.scheduler import runtime as scheduler_module
from app.scheduler.enqueue import (
    ScheduledScanEnqueueResult,
    enqueue_due_scan_tasks,
)
from app.scheduler.runtime import (
    SchedulerProcessConfig,
    SchedulerRuntime,
    _log_error,
    build_scheduler_runtime,
    run_scheduler,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "scheduler-process.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def seed_due_subscription(session: Session) -> int:
    account = CloudAccount(
        provider="aliyundrive",
        name="scheduler-account",
        refresh_token="encrypted",
        status="active",
    )
    session.add(account)
    session.flush()
    subscription = Subscription(
        cloud_account_id=account.id,
        name="scheduler-test",
        provider="aliyundrive",
        share_url="https://www.alipan.com/s/scheduler-test",
        share_key="scheduler-test",
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
    return subscription.id


def test_scheduler_process_config_maps_validated_settings() -> None:
    config = SchedulerProcessConfig.from_settings(
        Settings(
            _env_file=None,
            scheduler_poll_seconds=2.5,
            scheduler_batch_size=25,
        )
    )

    assert config.poll_interval == timedelta(seconds=2.5)
    assert config.batch_size == 25


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scheduler_poll_seconds", 0),
        ("scheduler_batch_size", 0),
        ("scheduler_batch_size", 1001),
    ],
)
def test_scheduler_settings_reject_invalid_process_values(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_build_scheduler_runtime_uses_process_settings(
    sessions: sessionmaker[Session],
) -> None:
    runtime, config = build_scheduler_runtime(
        settings=Settings(
            _env_file=None,
            scheduler_poll_seconds=3,
            scheduler_batch_size=17,
        ),
        session_factory=sessions,
    )

    assert isinstance(runtime, SchedulerRuntime)
    assert config.poll_interval == timedelta(seconds=3)
    assert config.batch_size == 17


def test_explicit_scheduler_cycle_enqueues_due_scan(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription_id = seed_due_subscription(session)

    runtime = SchedulerRuntime(
        session_factory=sessions,
        batch_size=100,
        clock=lambda: NOW,
    )
    result = runtime.run_once()

    with sessions() as session:
        task = session.scalar(select(Task))
        subscription = session.get(Subscription, subscription_id)

        assert result.enqueued_count == 1
        assert task is not None
        assert task.type == "scan"
        assert task.payload_version == 1
        assert subscription is not None
        next_scan_at = subscription.next_scan_at
        assert next_scan_at is not None
        if next_scan_at.tzinfo is None:
            next_scan_at = next_scan_at.replace(tzinfo=UTC)
        assert next_scan_at == NOW + timedelta(minutes=30)


def test_failed_cycle_rolls_back_and_next_cycle_retries(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        subscription_id = seed_due_subscription(session)

    attempts = 0

    def fail_once(
        session: Session,
        *,
        scheduled_at: datetime,
        limit: int,
    ) -> ScheduledScanEnqueueResult:
        nonlocal attempts
        attempts += 1
        result = enqueue_due_scan_tasks(
            session,
            scheduled_at=scheduled_at,
            limit=limit,
        )
        if attempts == 1:
            raise RuntimeError("refresh_token=must-not-be-logged")
        return result

    runtime = SchedulerRuntime(
        session_factory=sessions,
        batch_size=100,
        enqueue=fail_once,
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="must-not-be-logged"):
        runtime.run_once()

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        assert subscription is not None
        next_scan_at = subscription.next_scan_at
        assert next_scan_at is not None
        if next_scan_at.tzinfo is None:
            next_scan_at = next_scan_at.replace(tzinfo=UTC)
        assert next_scan_at == NOW
        assert list(session.scalars(select(Task))) == []

    result = runtime.run_once()

    with sessions() as session:
        assert result.enqueued_count == 1
        assert len(list(session.scalars(select(Task)))) == 1


async def test_runtime_continues_after_failed_cycle(
    sessions: sessionmaker[Session],
) -> None:
    attempts = 0
    stop = asyncio.Event()
    errors: list[Exception] = []

    def fail_once(
        _session: Session,
        *,
        scheduled_at: datetime,
        limit: int,
    ) -> ScheduledScanEnqueueResult:
        nonlocal attempts
        attempts += 1
        assert scheduled_at == NOW
        assert limit == 10
        if attempts == 1:
            raise RuntimeError("temporary")
        return ScheduledScanEnqueueResult(0, 0, 0, ())

    runtime = SchedulerRuntime(
        session_factory=sessions,
        batch_size=10,
        enqueue=fail_once,
        clock=lambda: NOW,
    )

    def on_cycle(_result: ScheduledScanEnqueueResult) -> None:
        stop.set()

    await runtime.run(
        stop=stop,
        poll_interval=timedelta(milliseconds=1),
        on_cycle=on_cycle,
        on_error=errors.append,
    )

    assert attempts == 2
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


async def test_runtime_waits_between_cycles_without_busy_spinning(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    cycle_count = 0
    observed_timeouts: list[float] = []

    def empty_cycle(
        _session: Session,
        *,
        scheduled_at: datetime,
        limit: int,
    ) -> ScheduledScanEnqueueResult:
        nonlocal cycle_count
        cycle_count += 1
        return ScheduledScanEnqueueResult(0, 0, 0, ())

    async def capture_wait(awaitable: object, *, timeout: float) -> bool:
        observed_timeouts.append(timeout)
        awaitable.close()  # type: ignore[attr-defined]
        stop.set()
        return True

    monkeypatch.setattr(scheduler_module.asyncio, "wait_for", capture_wait)
    runtime = SchedulerRuntime(
        session_factory=sessions,
        batch_size=10,
        enqueue=empty_cycle,
        clock=lambda: NOW,
    )

    await runtime.run(
        stop=stop,
        poll_interval=timedelta(seconds=7),
    )

    assert cycle_count == 1
    assert observed_timeouts == [7]


async def test_run_scheduler_delegates_lifecycle_and_logs(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    runtime = SchedulerRuntime(
        session_factory=sessions,
        batch_size=10,
        enqueue=lambda *_args, **_kwargs: ScheduledScanEnqueueResult(0, 0, 0, ()),
        clock=lambda: NOW,
    )
    info_events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        scheduler_module.logger,
        "info",
        lambda *args: info_events.append(args),
    )

    async def stop_after_start() -> None:
        await asyncio.sleep(0)
        stop.set()

    stopper = asyncio.create_task(stop_after_start())
    await run_scheduler(
        runtime=runtime,
        config=SchedulerProcessConfig(
            poll_interval=timedelta(seconds=1),
            batch_size=10,
        ),
        settings=Settings(
            _env_file=None,
            background_execution_mode="process",
        ),
        stop=stop,
        install_signal_handlers=False,
    )
    await stopper

    assert ("scheduler_started",) in info_events
    assert ("scheduler_stopped",) in info_events


def test_scheduler_error_log_redacts_exception_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error_events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        scheduler_module.logger,
        "error",
        lambda *args: error_events.append(args),
    )

    _log_error(RuntimeError("refresh_token=secret"))

    assert error_events == [
        ("scheduler_cycle_failed error_type=%s", "RuntimeError")
    ]
    assert "secret" not in repr(error_events)
