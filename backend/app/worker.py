from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from types import FrameType

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.task_engine.handlers import TaskHandlerRegistry
from app.task_engine.scan_handler import ScanTaskHandler
from app.task_engine.transfer_handler import TransferTaskHandler
from app.task_engine.worker import (
    ExponentialBackoff,
    WorkerCycleResult,
    WorkerRuntime,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
SignalCleanup = Callable[[], None]


@dataclass(frozen=True)
class WorkerProcessConfig:
    poll_interval: timedelta
    lease_duration: timedelta
    heartbeat_interval: timedelta
    recovery_batch_size: int
    retry_backoff: ExponentialBackoff

    @classmethod
    def from_settings(cls, settings: Settings) -> WorkerProcessConfig:
        return cls(
            poll_interval=timedelta(seconds=settings.worker_poll_seconds),
            lease_duration=timedelta(seconds=settings.worker_lease_seconds),
            heartbeat_interval=timedelta(seconds=settings.worker_heartbeat_seconds),
            recovery_batch_size=settings.worker_recovery_batch_size,
            retry_backoff=ExponentialBackoff(
                base_delay=timedelta(seconds=settings.worker_retry_base_seconds),
                max_delay=timedelta(seconds=settings.worker_retry_max_seconds),
            ),
        )


def generate_worker_id() -> str:
    """Create an opaque identifier that remains stable for this process lifetime."""

    hostname = socket.gethostname() or "unknown-host"
    return f"{hostname}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def build_handler_registry(
    *,
    session_factory: SessionFactory | None = None,
) -> TaskHandlerRegistry:
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal

    registry = TaskHandlerRegistry()
    registry.register("scan", 1, ScanTaskHandler(session_factory=session_factory))
    registry.register(
        "transfer",
        1,
        TransferTaskHandler(session_factory=session_factory),
    )
    return registry


def build_worker_runtime(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    worker_id: str | None = None,
) -> tuple[WorkerRuntime, WorkerProcessConfig]:
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal

    resolved_settings = settings or get_settings()
    config = WorkerProcessConfig.from_settings(resolved_settings)
    runtime = WorkerRuntime(
        session_factory=session_factory,
        handlers=build_handler_registry(session_factory=session_factory),
        worker_id=worker_id or generate_worker_id(),
        lease_duration=config.lease_duration,
        heartbeat_interval=config.heartbeat_interval,
        recovery_limit=config.recovery_batch_size,
        backoff=config.retry_backoff,
    )
    return runtime, config


def _install_signal_handlers(
    stop: asyncio.Event,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SignalCleanup:
    event_loop = loop or asyncio.get_running_loop()
    installed_on_loop: list[signal.Signals] = []
    previous_handlers: dict[
        signal.Signals,
        signal.Handlers,
    ] = {}

    def request_stop(signal_name: str) -> None:
        logger.info("worker_stop_requested signal=%s", signal_name)
        stop.set()

    for process_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            event_loop.add_signal_handler(
                process_signal,
                request_stop,
                process_signal.name,
            )
            installed_on_loop.append(process_signal)
        except (NotImplementedError, RuntimeError):
            previous_handlers[process_signal] = signal.getsignal(process_signal)

            def fallback_handler(
                _signum: int,
                _frame: FrameType | None,
                *,
                signal_name: str = process_signal.name,
            ) -> None:
                request_stop(signal_name)

            signal.signal(process_signal, fallback_handler)

    def cleanup() -> None:
        for process_signal in installed_on_loop:
            event_loop.remove_signal_handler(process_signal)
        for process_signal, previous_handler in previous_handlers.items():
            signal.signal(process_signal, previous_handler)

    return cleanup


def _log_cycle(worker_id: str, result: WorkerCycleResult) -> None:
    if result.recovered_count:
        logger.info(
            "worker_recovery worker_id=%s recovered_count=%d",
            worker_id,
            result.recovered_count,
        )
    if result.status == "completed":
        logger.info(
            "worker_cycle_completed worker_id=%s task_id=%s task_status=%s",
            worker_id,
            result.task_id,
            result.task_status,
        )
    elif result.status == "ownership_lost":
        logger.warning(
            "worker_ownership_lost worker_id=%s task_id=%s",
            worker_id,
            result.task_id,
        )


async def run_worker(
    *,
    runtime: WorkerRuntime | None = None,
    config: WorkerProcessConfig | None = None,
    worker_id: str | None = None,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> None:
    resolved_worker_id = worker_id or generate_worker_id()
    if runtime is None:
        runtime, built_config = build_worker_runtime(worker_id=resolved_worker_id)
        config = config or built_config
    if config is None:
        config = WorkerProcessConfig.from_settings(get_settings())

    stop_event = stop or asyncio.Event()

    def cleanup_signals() -> None:
        pass

    if install_signal_handlers:
        cleanup_signals = _install_signal_handlers(stop_event)

    logger.info("worker_started worker_id=%s", resolved_worker_id)
    try:
        await runtime.run(
            stop=stop_event,
            idle_poll_interval=config.poll_interval,
            on_cycle=lambda result: _log_cycle(resolved_worker_id, result),
        )
    finally:
        cleanup_signals()
        logger.info("worker_stopped worker_id=%s", resolved_worker_id)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
