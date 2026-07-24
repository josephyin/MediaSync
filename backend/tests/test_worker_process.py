import asyncio
import re
import signal
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import worker as worker_module
from app.core.config import Settings
from app.models import Base, Task
from app.repositories import TaskRepository
from app.task_engine import TaskExecutionContext, TaskHandlerRegistry, TaskOutcome
from app.task_engine.scan_handler import ScanTaskHandler
from app.task_engine.transfer_handler import TransferTaskHandler
from app.task_engine.worker import WorkerCycleResult, WorkerRuntime
from app.worker import (
    WorkerProcessConfig,
    _install_signal_handlers,
    _log_cycle,
    build_handler_registry,
    generate_worker_id,
    run_worker,
)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "worker-process.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_worker_settings_require_heartbeat_shorter_than_lease() -> None:
    with pytest.raises(
        ValidationError,
        match="worker_heartbeat_seconds must be shorter",
    ):
        Settings(
            _env_file=None,
            worker_lease_seconds=20,
            worker_heartbeat_seconds=20,
        )


def test_worker_settings_require_bounded_retry_base() -> None:
    with pytest.raises(
        ValidationError,
        match="worker_retry_max_seconds must not be shorter",
    ):
        Settings(
            _env_file=None,
            worker_retry_base_seconds=60,
            worker_retry_max_seconds=30,
        )


def test_worker_process_config_maps_validated_settings() -> None:
    config = WorkerProcessConfig.from_settings(
        Settings(
            _env_file=None,
            worker_poll_seconds=2.5,
            worker_lease_seconds=90,
            worker_heartbeat_seconds=30,
            worker_recovery_batch_size=25,
            worker_retry_base_seconds=10,
            worker_retry_max_seconds=120,
        )
    )

    assert config.poll_interval == timedelta(seconds=2.5)
    assert config.lease_duration == timedelta(seconds=90)
    assert config.heartbeat_interval == timedelta(seconds=30)
    assert config.recovery_batch_size == 25
    assert config.retry_backoff.base_delay == timedelta(seconds=10)
    assert config.retry_backoff.max_delay == timedelta(seconds=120)


def test_production_registry_contains_only_supported_v1_handlers(
    sessions: sessionmaker[Session],
) -> None:
    registry = build_handler_registry(session_factory=sessions)

    assert isinstance(registry.resolve("scan", 1), ScanTaskHandler)
    assert isinstance(registry.resolve("transfer", 1), TransferTaskHandler)
    assert registry.resolve("scan", 2) is None
    assert registry.resolve("unknown", 1) is None


def test_generated_worker_id_is_opaque_and_process_scoped() -> None:
    first = generate_worker_id()
    second = generate_worker_id()

    assert first != second
    assert re.fullmatch(r".+-\d+-[0-9a-f]{8}", first)


async def test_signal_handler_requests_graceful_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    callbacks: dict[signal.Signals, tuple[object, tuple[object, ...]]] = {}
    removed: list[signal.Signals] = []

    def add_handler(
        process_signal: signal.Signals,
        callback: object,
        *args: object,
    ) -> None:
        callbacks[process_signal] = (callback, args)

    monkeypatch.setattr(loop, "add_signal_handler", add_handler)
    monkeypatch.setattr(
        loop,
        "remove_signal_handler",
        lambda process_signal: removed.append(process_signal) or True,
    )

    cleanup = _install_signal_handlers(stop, loop=loop)
    callback, args = callbacks[signal.SIGTERM]
    callback(*args)  # type: ignore[operator]

    assert stop.is_set()
    cleanup()
    assert removed == [signal.SIGTERM, signal.SIGINT]


async def test_explicit_worker_command_processes_a_queued_task(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = TaskRepository(session).create_task(
            Task(type="test", payload_version=1, payload={})
        )
        task_id = task.id

    stop = asyncio.Event()
    handlers = TaskHandlerRegistry()

    async def handler(_context: TaskExecutionContext) -> TaskOutcome:
        stop.set()
        return TaskOutcome(status="success", summary="processed by process entrypoint")

    handlers.register("test", 1, handler)
    runtime = WorkerRuntime(
        session_factory=sessions,
        handlers=handlers,
        worker_id="worker-process-test",
    )
    config = WorkerProcessConfig.from_settings(Settings(_env_file=None))

    await run_worker(
        runtime=runtime,
        config=config,
        worker_id="worker-process-test",
        stop=stop,
        install_signal_handlers=False,
    )

    with sessions() as session:
        persisted = session.get(Task, task_id)
        assert persisted is not None
        assert persisted.status == "success"


def test_cycle_logging_covers_recovery_completion_and_ownership_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info_events: list[tuple[object, ...]] = []
    warning_events: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        worker_module.logger,
        "info",
        lambda *args: info_events.append(args),
    )
    monkeypatch.setattr(
        worker_module.logger,
        "warning",
        lambda *args: warning_events.append(args),
    )

    _log_cycle(
        "worker-a",
        WorkerCycleResult(
            status="completed",
            recovered_count=2,
            task_id=7,
            task_status="success",
        ),
    )
    _log_cycle(
        "worker-a",
        WorkerCycleResult(
            status="ownership_lost",
            recovered_count=0,
            task_id=8,
        ),
    )

    assert ("worker_recovery worker_id=%s recovered_count=%d", "worker-a", 2) in info_events
    assert (
        "worker_cycle_completed worker_id=%s task_id=%s task_status=%s",
        "worker-a",
        7,
        "success",
    ) in info_events
    assert (
        "worker_ownership_lost worker_id=%s task_id=%s",
        "worker-a",
        8,
    ) in warning_events
