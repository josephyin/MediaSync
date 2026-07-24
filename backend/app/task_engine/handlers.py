from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from types import MappingProxyType
from typing import Literal, Protocol

from app.models import Task

TaskOutcomeStatus = Literal[
    "success",
    "retry",
    "failed",
    "waiting_credential",
    "cancelled",
]


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return deepcopy(value)


@dataclass(frozen=True)
class TaskInvocation:
    task_id: int
    task_run_id: int
    task_type: str
    payload_version: int
    payload: Mapping[str, object]
    trigger_type: str
    account_id: int | None
    subscription_id: int | None
    file_id: int | None
    claimed_status: str
    retry_count: int
    max_retries: int

    @classmethod
    def from_task(cls, task: Task, *, task_run_id: int) -> TaskInvocation:
        payload = MappingProxyType(
            {
                str(key): _freeze_json(value)
                for key, value in (task.payload or {}).items()
            }
        )
        return cls(
            task_id=task.id,
            task_run_id=task_run_id,
            task_type=task.type,
            payload_version=task.payload_version,
            payload=payload,
            trigger_type=task.trigger_type,
            account_id=task.account_id,
            subscription_id=task.subscription_id,
            file_id=task.file_id,
            claimed_status=task.status,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
        )


@dataclass(frozen=True)
class TaskExecutionContext:
    task: TaskInvocation
    worker_id: str
    lock_token: str
    _cancellation_probe: Callable[[], Awaitable[bool]] = field(
        repr=False,
        compare=False,
    )

    async def cancellation_requested(self) -> bool:
        return await self._cancellation_probe()


@dataclass(frozen=True)
class TaskOutcome:
    status: TaskOutcomeStatus
    summary: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    metrics: Mapping[str, object] | None = None
    retry_after: timedelta | None = None
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if self.retry_after is not None and self.retry_after <= timedelta(0):
            raise ValueError("retry_after must be positive")
        if self.status == "retry" and self.error_code is None:
            raise ValueError("retry outcome requires error_code")
        if self.status == "waiting_credential" and self.blocked_reason is None:
            raise ValueError("waiting_credential outcome requires blocked_reason")
        if self.status != "retry" and self.retry_after is not None:
            raise ValueError("retry_after is only valid for retry outcomes")
        if self.status != "waiting_credential" and self.blocked_reason is not None:
            raise ValueError(
                "blocked_reason is only valid for waiting_credential outcomes"
            )


class TaskHandler(Protocol):
    async def __call__(self, context: TaskExecutionContext) -> TaskOutcome: ...


class TaskHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, int], TaskHandler] = {}

    def register(
        self,
        task_type: str,
        payload_version: int,
        handler: TaskHandler,
    ) -> None:
        if not task_type:
            raise ValueError("task_type must not be empty")
        if payload_version < 1:
            raise ValueError("payload_version must be positive")
        key = (task_type, payload_version)
        if key in self._handlers:
            raise ValueError(
                f"handler already registered for {task_type!r} payload v{payload_version}"
            )
        self._handlers[key] = handler

    def resolve(self, task_type: str, payload_version: int) -> TaskHandler | None:
        return self._handlers.get((task_type, payload_version))
