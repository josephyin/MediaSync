from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.execution import (
    BackgroundExecutionModeError,
    require_background_execution_mode,
)
from app.core.process import install_shutdown_signal_handlers
from app.models.base import utcnow
from app.scheduler.enqueue import (
    ScheduledScanEnqueueResult,
    enqueue_due_scan_tasks,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]
EnqueueOperation = Callable[..., ScheduledScanEnqueueResult]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class SchedulerProcessConfig:
    poll_interval: timedelta
    batch_size: int

    @classmethod
    def from_settings(cls, settings: Settings) -> SchedulerProcessConfig:
        return cls(
            poll_interval=timedelta(seconds=settings.scheduler_poll_seconds),
            batch_size=settings.scheduler_batch_size,
        )


class SchedulerRuntime:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        batch_size: int,
        enqueue: EnqueueOperation = enqueue_due_scan_tasks,
        clock: Clock = utcnow,
    ) -> None:
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._enqueue = enqueue
        self._clock = clock

    def run_once(self) -> ScheduledScanEnqueueResult:
        with self._session_factory() as session, session.begin():
            return self._enqueue(
                session,
                scheduled_at=self._clock(),
                limit=self._batch_size,
            )

    async def run(
        self,
        *,
        stop: asyncio.Event,
        poll_interval: timedelta,
        on_cycle: Callable[[ScheduledScanEnqueueResult], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be positive")

        while not stop.is_set():
            try:
                result = await asyncio.to_thread(self.run_once)
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
            else:
                if on_cycle is not None:
                    on_cycle(result)

            if stop.is_set():
                return
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=poll_interval.total_seconds(),
                )
            except TimeoutError:
                pass


def build_scheduler_runtime(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
) -> tuple[SchedulerRuntime, SchedulerProcessConfig]:
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal

    resolved_settings = settings or get_settings()
    config = SchedulerProcessConfig.from_settings(resolved_settings)
    runtime = SchedulerRuntime(
        session_factory=session_factory,
        batch_size=config.batch_size,
    )
    return runtime, config


def _log_cycle(result: ScheduledScanEnqueueResult) -> None:
    logger.info(
        "scheduler_cycle_completed inspected_count=%d enqueued_count=%d "
        "skipped_active_count=%d",
        result.inspected_count,
        result.enqueued_count,
        result.skipped_active_count,
    )


def _log_error(error: Exception) -> None:
    logger.error(
        "scheduler_cycle_failed error_type=%s",
        type(error).__name__,
    )


async def run_scheduler(
    *,
    runtime: SchedulerRuntime | None = None,
    config: SchedulerProcessConfig | None = None,
    settings: Settings | None = None,
    stop: asyncio.Event | None = None,
    install_signal_handlers: bool = True,
) -> None:
    resolved_settings = settings or get_settings()
    logger.info(
        "background_execution_mode_selected process=scheduler mode=%s",
        resolved_settings.background_execution_mode,
    )
    require_background_execution_mode(
        resolved_settings,
        process_name="scheduler",
        expected_mode="process",
    )

    if runtime is None:
        runtime, built_config = build_scheduler_runtime(settings=resolved_settings)
        config = config or built_config
    if config is None:
        config = SchedulerProcessConfig.from_settings(resolved_settings)

    stop_event = stop or asyncio.Event()

    def cleanup_signals() -> None:
        pass

    if install_signal_handlers:
        cleanup_signals = install_shutdown_signal_handlers(
            stop_event,
            logger=logger,
            process_name="scheduler",
        )

    logger.info("scheduler_started")
    try:
        await runtime.run(
            stop=stop_event,
            poll_interval=config.poll_interval,
            on_cycle=_log_cycle,
            on_error=_log_error,
        )
    finally:
        cleanup_signals()
        logger.info("scheduler_stopped")


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(run_scheduler(settings=settings))
    except BackgroundExecutionModeError as exc:
        logger.error(
            "process_start_refused process=%s expected_mode=%s actual_mode=%s",
            exc.process_name,
            exc.expected_mode,
            exc.actual_mode,
        )
        raise SystemExit(2) from None
