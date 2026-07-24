from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy.orm import Session

from app.models import Task
from app.models.base import utcnow
from app.repositories import TaskOwnershipLostError, TaskRepository
from app.task_engine.handlers import (
    TaskExecutionContext,
    TaskHandlerRegistry,
    TaskInvocation,
    TaskOutcome,
)

WorkerCycleStatus = Literal["idle", "completed", "ownership_lost"]


class InvalidTaskOutcomeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExponentialBackoff:
    base_delay: timedelta = timedelta(seconds=30)
    max_delay: timedelta = timedelta(minutes=15)

    def __post_init__(self) -> None:
        if self.base_delay <= timedelta(0):
            raise ValueError("base_delay must be positive")
        if self.max_delay < self.base_delay:
            raise ValueError("max_delay must not be shorter than base_delay")

    def delay_for(self, retry_count: int) -> timedelta:
        if retry_count < 0:
            raise ValueError("retry_count must not be negative")
        delay = self.base_delay
        for _ in range(retry_count):
            if delay >= self.max_delay / 2:
                return self.max_delay
            delay *= 2
        return min(delay, self.max_delay)

    def bounded(self, delay: timedelta) -> timedelta:
        if delay <= timedelta(0):
            raise ValueError("delay must be positive")
        return min(delay, self.max_delay)


@dataclass(frozen=True)
class WorkerCycleResult:
    status: WorkerCycleStatus
    recovered_count: int
    task_id: int | None = None
    task_status: str | None = None


@dataclass(frozen=True)
class _ClaimedExecution:
    invocation: TaskInvocation
    lock_token: str


class WorkerRuntime:
    """Single-concurrency Task Engine v2 runtime.

    Database work always uses short-lived sessions. Handlers only receive an
    immutable invocation and cannot mutate Task or TaskRun execution state.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        handlers: TaskHandlerRegistry,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=60),
        heartbeat_interval: timedelta = timedelta(seconds=20),
        recovery_limit: int = 100,
        backoff: ExponentialBackoff | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration must be positive")
        if heartbeat_interval <= timedelta(0):
            raise ValueError("heartbeat_interval must be positive")
        if heartbeat_interval >= lease_duration:
            raise ValueError("heartbeat_interval must be shorter than lease_duration")
        if recovery_limit < 1 or recovery_limit > 1000:
            raise ValueError("recovery_limit must be between 1 and 1000")
        self._session_factory = session_factory
        self._handlers = handlers
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._heartbeat_interval = heartbeat_interval
        self._recovery_limit = recovery_limit
        self._backoff = backoff or ExponentialBackoff()
        self._clock = clock

    async def run_once(self) -> WorkerCycleResult:
        claimed, recovered_count = await asyncio.to_thread(self._recover_and_claim)
        if claimed is None:
            return WorkerCycleResult(status="idle", recovered_count=recovered_count)

        invocation = claimed.invocation
        handler = self._handlers.resolve(
            invocation.task_type,
            invocation.payload_version,
        )
        if handler is None:
            outcome = TaskOutcome(
                status="failed",
                summary="No compatible task handler is registered",
                error_code="UNSUPPORTED_TASK_HANDLER",
                error_message=(
                    f"unsupported task type {invocation.task_type!r} "
                    f"payload v{invocation.payload_version}"
                ),
            )
            return await self._finalize(
                claimed,
                outcome,
                duration_ms=0,
                recovered_count=recovered_count,
            )

        context = TaskExecutionContext(
            task=invocation,
            worker_id=self._worker_id,
            lock_token=claimed.lock_token,
            _cancellation_probe=lambda: self._cancellation_requested(claimed),
        )
        heartbeat_stop = asyncio.Event()
        ownership_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                claimed,
                stop=heartbeat_stop,
                ownership_lost=ownership_lost,
            )
        )
        started = time.perf_counter()
        try:
            try:
                outcome = await handler(context)
            except Exception as exc:
                outcome = TaskOutcome(
                    status="failed",
                    summary="Task handler raised an exception",
                    error_code="TASK_HANDLER_EXCEPTION",
                    error_message=f"task handler raised {type(exc).__name__}",
                )
        finally:
            heartbeat_stop.set()
            await heartbeat_task

        duration_ms = max(0, round((time.perf_counter() - started) * 1000))
        if ownership_lost.is_set():
            return WorkerCycleResult(
                status="ownership_lost",
                recovered_count=recovered_count,
                task_id=invocation.task_id,
            )
        return await self._finalize(
            claimed,
            outcome,
            duration_ms=duration_ms,
            recovered_count=recovered_count,
        )

    async def run(
        self,
        *,
        stop: asyncio.Event,
        idle_poll_interval: timedelta = timedelta(seconds=1),
        on_cycle: Callable[[WorkerCycleResult], None] | None = None,
    ) -> None:
        if idle_poll_interval <= timedelta(0):
            raise ValueError("idle_poll_interval must be positive")
        while not stop.is_set():
            result = await self.run_once()
            if on_cycle is not None:
                on_cycle(result)
            if result.status != "idle":
                continue
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=idle_poll_interval.total_seconds(),
                )
            except TimeoutError:
                pass

    def _recover_and_claim(self) -> tuple[_ClaimedExecution | None, int]:
        now = self._clock()
        with self._session_factory() as session, session.begin():
            repository = TaskRepository(session, clock=self._clock)
            leases = repository.list_expired_leases(
                limit=self._recovery_limit,
                expired_at=now,
            )
            recovered_count = 0
            for lease in leases:
                recovered = repository.recover_expired_lease(
                    lease,
                    next_attempt_at=now + self._backoff.delay_for(lease.retry_count),
                    recovered_at=now,
                )
                if recovered is not None:
                    recovered_count += 1

            claim = repository.claim_next(
                worker_id=self._worker_id,
                lease_duration=self._lease_duration,
                claimed_at=now,
            )
            if claim is None:
                return None, recovered_count
            return (
                _ClaimedExecution(
                    invocation=TaskInvocation.from_task(
                        claim.task,
                        task_run_id=claim.task_run.id,
                    ),
                    lock_token=claim.lock_token,
                ),
                recovered_count,
            )

    async def _heartbeat_loop(
        self,
        claimed: _ClaimedExecution,
        *,
        stop: asyncio.Event,
        ownership_lost: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(
                    stop.wait(),
                    timeout=self._heartbeat_interval.total_seconds(),
                )
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(self._heartbeat, claimed)
            except TaskOwnershipLostError:
                ownership_lost.set()
                return

    def _heartbeat(self, claimed: _ClaimedExecution) -> None:
        with self._session_factory() as session, session.begin():
            TaskRepository(session, clock=self._clock).heartbeat(
                claimed.invocation.task_id,
                worker_id=self._worker_id,
                lock_token=claimed.lock_token,
                lease_duration=self._lease_duration,
                heartbeat_at=self._clock(),
            )

    async def _cancellation_requested(self, claimed: _ClaimedExecution) -> bool:
        return await asyncio.to_thread(self._read_cancellation, claimed)

    def _read_cancellation(self, claimed: _ClaimedExecution) -> bool:
        with self._session_factory() as session:
            task = session.get(Task, claimed.invocation.task_id)
            if (
                task is None
                or task.locked_by != self._worker_id
                or task.lock_token != claimed.lock_token
            ):
                raise TaskOwnershipLostError(
                    claimed.invocation.task_id,
                    self._worker_id,
                )
            return task.status == "cancel_requested"

    async def _finalize(
        self,
        claimed: _ClaimedExecution,
        outcome: TaskOutcome,
        *,
        duration_ms: int,
        recovered_count: int,
    ) -> WorkerCycleResult:
        try:
            task_status = await asyncio.to_thread(
                self._finish,
                claimed,
                outcome,
                duration_ms,
            )
        except TaskOwnershipLostError:
            return WorkerCycleResult(
                status="ownership_lost",
                recovered_count=recovered_count,
                task_id=claimed.invocation.task_id,
            )
        return WorkerCycleResult(
            status="completed",
            recovered_count=recovered_count,
            task_id=claimed.invocation.task_id,
            task_status=task_status,
        )

    def _finish(
        self,
        claimed: _ClaimedExecution,
        outcome: TaskOutcome,
        duration_ms: int,
    ) -> str:
        now = self._clock()
        with self._session_factory() as session, session.begin():
            task = session.get(Task, claimed.invocation.task_id)
            if (
                task is None
                or task.locked_by != self._worker_id
                or task.lock_token != claimed.lock_token
            ):
                raise TaskOwnershipLostError(
                    claimed.invocation.task_id,
                    self._worker_id,
                )
            expected_status = task.status
            if expected_status == "cancel_requested" and outcome.status not in {
                "success",
                "cancelled",
            }:
                raise InvalidTaskOutcomeError(
                    "cancel_requested task must reconcile to success or cancelled"
                )

            task_status = outcome.status
            run_status = outcome.status
            consume_retry_budget = outcome.status in {"retry", "failed"}
            next_attempt_at = None
            if outcome.status == "retry":
                next_retry_count = task.retry_count + 1
                if next_retry_count > task.max_retries:
                    task_status = "failed"
                else:
                    retry_after = outcome.retry_after or self._backoff.delay_for(
                        task.retry_count
                    )
                    next_attempt_at = now + self._backoff.bounded(retry_after)
                run_status = "failed"
            elif outcome.status == "waiting_credential":
                run_status = "blocked"

            repository = TaskRepository(session, clock=self._clock)
            persisted_task, _persisted_run = repository.finish_run(
                task.id,
                claimed.invocation.task_run_id,
                worker_id=self._worker_id,
                lock_token=claimed.lock_token,
                expected_task_status=expected_status,
                task_status=task_status,
                run_status=run_status,
                finished_at=now,
                duration_ms=duration_ms,
                result_summary=outcome.summary,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
                metrics=dict(outcome.metrics) if outcome.metrics is not None else None,
                blocked_reason=outcome.blocked_reason,
                next_attempt_at=next_attempt_at,
                consume_retry_budget=consume_retry_budget,
            )
            return persisted_task.status
