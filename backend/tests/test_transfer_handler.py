from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ProviderRequestError, ProviderWriteUncertainError
from app.models import Base, CloudAccount, CloudFile, Subscription, Task, TaskRun
from app.providers.base import (
    FolderRef,
    RemoteItem,
    SaveOperation,
    SaveResult,
    ShareInfo,
)
from app.task_engine.handlers import (
    TaskExecutionContext,
    TaskHandlerRegistry,
    TaskInvocation,
)
from app.task_engine.transfer_handler import TransferTaskHandler
from app.task_engine.worker import WorkerRuntime

NOW = datetime(2026, 7, 24, 9, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "transfer-handler.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


class FakeTransferProvider:
    def __init__(
        self,
        *,
        existing: RemoteItem | None = None,
        resolve_error: Exception | None = None,
        on_save: Callable[[], None] | None = None,
    ) -> None:
        self.existing = existing
        self.resolve_error = resolve_error
        self.on_save = on_save
        self.request_count = 4
        self.ensure_calls: list[tuple[str, str]] = []
        self.save_calls = 0

    async def resolve_share(
        self,
        share_url: str,
        password: str | None = None,
    ) -> ShareInfo:
        if self.resolve_error is not None:
            raise self.resolve_error
        assert share_url == "https://www.alipan.com/s/share-1"
        assert password is None
        return ShareInfo(share_key="share-1", name="Share")

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        self.ensure_calls.append((parent.folder_id, name))
        return FolderRef(
            folder_id=f"folder-{name}",
            path=f"{parent.path.rstrip('/')}/{name}",
        )

    async def find_target_item(
        self,
        target: FolderRef,
        name: str,
    ) -> RemoteItem | None:
        return self.existing

    async def save_shared_item(
        self,
        share: ShareInfo,
        source: RemoteItem,
        target: FolderRef,
    ) -> SaveResult:
        self.save_calls += 1
        if self.on_save is not None:
            self.on_save()
        return SaveResult(
            target_file_id="saved-file-1",
            target_path=f"{target.path}/{source.filename}",
        )


class FakeResumableTransferProvider(FakeTransferProvider):
    def __init__(self, *, completed: bool = False, uncertain: bool = False) -> None:
        super().__init__()
        self.completed = completed
        self.uncertain = uncertain
        self.start_calls = 0
        self.query_calls = 0

    async def start_save_shared_item(
        self,
        _share: ShareInfo,
        _source: RemoteItem,
        _target: FolderRef,
    ) -> str:
        self.start_calls += 1
        if self.uncertain:
            raise ProviderWriteUncertainError("request timed out with secret-token")
        return "provider-task-1"

    async def query_save_operation(self, operation_id: str) -> SaveOperation:
        self.query_calls += 1
        assert operation_id == "provider-task-1"
        return SaveOperation(
            operation_id=operation_id,
            completed=self.completed,
            target_file_ids=("saved-file-1",) if self.completed else (),
        )


def seed_transfer(
    sessions: sessionmaker[Session],
    *,
    target_path: str = "/Media",
    relative_path: str = "Movies/movie.mkv",
) -> tuple[int, int, int]:
    with sessions() as session, session.begin():
        account = CloudAccount(
            provider="aliyundrive",
            name="test",
            refresh_token="encrypted",
            status="active",
        )
        session.add(account)
        session.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            name="test",
            provider="aliyundrive",
            share_url="https://www.alipan.com/s/share-1",
            target_path=target_path,
            schedule="interval:30m",
        )
        session.add(subscription)
        session.flush()
        file = CloudFile(
            subscription_id=subscription.id,
            remote_file_id="source-file-1",
            parent_remote_file_id="source-parent",
            filename="movie.mkv",
            relative_path=relative_path,
            item_type="file",
            size=1024,
            content_hash="sha1",
            fingerprint="source-file-1:1024",
            status="pending",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        session.add(file)
        session.flush()
        task = Task(
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            status="pending",
            payload_version=1,
        )
        session.add(task)
        session.flush()
        return task.id, subscription.id, file.id


def context(
    *,
    task_id: int,
    subscription_id: int,
    file_id: int,
    cancellation_probe: Callable[[], object] | None = None,
) -> TaskExecutionContext:
    async def not_cancelled() -> bool:
        return False

    async def probe() -> bool:
        assert cancellation_probe is not None
        return bool(cancellation_probe())

    return TaskExecutionContext(
        task=TaskInvocation(
            task_id=task_id,
            task_run_id=999,
            task_type="transfer",
            payload_version=1,
            payload={},
            trigger_type="scheduled",
            account_id=None,
            subscription_id=subscription_id,
            file_id=file_id,
            claimed_status="running",
            retry_count=0,
            max_retries=3,
        ),
        worker_id="worker-a",
        lock_token="lock-token",
        _cancellation_probe=probe if cancellation_probe is not None else not_cancelled,
    )


def handler(
    sessions: sessionmaker[Session],
    provider: FakeTransferProvider,
    *,
    token_persister=None,
) -> TransferTaskHandler:
    return TransferTaskHandler(
        session_factory=sessions,
        provider_factory=lambda *_args: provider,  # type: ignore[arg-type]
        token_loader=lambda _account: "refresh-token",
        token_persister=token_persister or (lambda *_args: False),
        clock=lambda: NOW,
    )


def load_state(
    sessions: sessionmaker[Session],
    task_id: int,
    file_id: int,
) -> tuple[Task, CloudFile, int]:
    with sessions() as session:
        task = session.get(Task, task_id)
        file = session.get(CloudFile, file_id)
        run_count = session.scalar(
            select(func.count()).select_from(TaskRun).where(TaskRun.task_id == task_id)
        )
        assert task is not None
        assert file is not None
        session.expunge(task)
        session.expunge(file)
        return task, file, run_count or 0


async def test_transfer_handler_saves_file_without_mutating_task_state(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    provider = FakeTransferProvider()

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
        )
    )
    task, file, run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "success"
    assert outcome.metrics == {
        "already_existed": False,
        "provider_request_count": 4,
    }
    assert provider.ensure_calls == [
        ("root", "Media"),
        ("folder-Media", "Movies"),
    ]
    assert provider.save_calls == 1
    assert file.status == "saved"
    assert file.target_file_id == "saved-file-1"
    assert file.target_path == "/Media/Movies/movie.mkv"
    assert file.saved_at == NOW.replace(tzinfo=None)
    assert task.status == "pending"
    assert run_count == 0


async def test_transfer_handler_integrates_with_fenced_worker_runtime(
    sessions: sessionmaker[Session],
) -> None:
    task_id, _subscription_id, file_id = seed_transfer(sessions)
    provider = FakeTransferProvider()
    handlers = TaskHandlerRegistry()
    handlers.register("transfer", 1, handler(sessions, provider))
    worker = WorkerRuntime(
        session_factory=sessions,
        handlers=handlers,
        worker_id="worker-a",
        clock=lambda: NOW,
    )

    result = await worker.run_once()
    task, file, run_count = load_state(sessions, task_id, file_id)

    assert result.status == "completed"
    assert result.task_status == "success"
    assert task.status == "success"
    assert file.status == "saved"
    assert run_count == 1


async def test_resumable_transfer_persists_operation_and_resumes_without_resubmit(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    provider = FakeResumableTransferProvider()
    transfer_handler = handler(sessions, provider)

    first = await transfer_handler(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
        )
    )
    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.provider_write_intent_at == NOW.replace(tzinfo=None)
        assert task.provider_operation_id == "provider-task-1"
        assert task.provider_operation_status == "pending"

    provider.completed = True
    resumed_context = context(
        task_id=task_id,
        subscription_id=subscription_id,
        file_id=file_id,
    )
    resumed_context = replace(
        resumed_context,
        task=replace(
            resumed_context.task,
            provider_operation_id="provider-task-1",
            provider_operation_status="pending",
        ),
    )
    second = await transfer_handler(resumed_context)
    task, file, _run_count = load_state(sessions, task_id, file_id)

    assert first.status == "retry"
    assert first.error_code == "PROVIDER_OPERATION_PENDING"
    assert second.status == "success"
    assert provider.start_calls == 1
    assert provider.query_calls == 2
    assert task.provider_operation_status == "succeeded"
    assert task.provider_result == {
        "target_file_id": "saved-file-1",
        "target_path": "/Media/Movies/movie.mkv",
    }
    assert file.status == "saved"


async def test_uncertain_write_is_terminal_and_never_contains_secret(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    provider = FakeResumableTransferProvider(uncertain=True)

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
        )
    )
    task, file, _run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "PROVIDER_WRITE_UNCERTAIN"
    assert "secret-token" not in (outcome.error_message or "")
    assert task.provider_operation_status == "uncertain"
    assert task.provider_operation_id is None
    assert file.status == "failed"
    assert provider.start_calls == 1


async def test_existing_destination_is_reconciled_without_copy(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    provider = FakeTransferProvider(
        existing=RemoteItem(
            remote_file_id="existing-file-1",
            parent_id="target",
            filename="movie.mkv",
            item_type="file",
        )
    )

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
        )
    )
    _task, file, _run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "success"
    assert outcome.metrics is not None
    assert outcome.metrics["already_existed"] is True
    assert provider.save_calls == 0
    assert file.target_file_id == "existing-file-1"


async def test_cancellation_before_provider_access_skips_remote_calls(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    provider = FakeTransferProvider()

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
            cancellation_probe=lambda: True,
        )
    )
    task, file, run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "cancelled"
    assert provider.ensure_calls == []
    assert provider.save_calls == 0
    assert file.status == "pending"
    assert file.last_error == "transfer cancelled"
    assert task.status == "pending"
    assert run_count == 0


async def test_cancellation_before_copy_does_not_save_remote_item(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(
        sessions,
        target_path="/",
        relative_path="movie.mkv",
    )
    provider = FakeTransferProvider()
    decisions = iter((False, False, True))

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
            cancellation_probe=lambda: next(decisions),
        )
    )
    _task, file, _run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "cancelled"
    assert provider.save_calls == 0
    assert file.status == "pending"


async def test_confirmed_remote_success_wins_over_late_cancellation(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(
        sessions,
        target_path="/",
        relative_path="movie.mkv",
    )
    cancelled = False

    def mark_cancelled() -> None:
        nonlocal cancelled
        cancelled = True

    provider = FakeTransferProvider(on_save=mark_cancelled)

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
            cancellation_probe=lambda: cancelled,
        )
    )
    _task, file, _run_count = load_state(sessions, task_id, file_id)

    assert cancelled
    assert outcome.status == "success"
    assert file.status == "saved"


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (
            RuntimeError("provider response contains secret-token"),
            "retry",
            "TRANSFER_EXECUTION_FAILED",
        ),
        (
            ProviderRequestError(
                "Aliyun Drive InvalidParameter.RefreshToken: secret-token"
            ),
            "waiting_credential",
            "CREDENTIAL_INVALID",
        ),
    ],
)
async def test_transfer_errors_are_classified_and_sanitized(
    sessions: sessionmaker[Session],
    error: Exception,
    expected_status: str,
    expected_code: str,
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    provider = FakeTransferProvider(resolve_error=error)

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
        )
    )
    _task, file, _run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == expected_status
    assert outcome.error_code == expected_code
    assert "secret-token" not in (outcome.error_message or "")
    assert "secret-token" not in (file.last_error or "")
    assert file.status == "pending"
    if expected_status == "waiting_credential":
        assert outcome.blocked_reason == "cloud-drive credential requires user action"


async def test_missing_transfer_source_is_terminal_and_does_not_touch_task(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)

    outcome = await handler(sessions, FakeTransferProvider())(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id + 100,
        )
    )
    task, file, run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "TRANSFER_SOURCE_NOT_FOUND"
    assert task.status == "pending"
    assert file.status == "pending"
    assert run_count == 0


async def test_transfer_payload_v1_rejects_extra_fields(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)
    valid_context = context(
        task_id=task_id,
        subscription_id=subscription_id,
        file_id=file_id,
    )
    invalid_context = replace(
        valid_context,
        task=replace(valid_context.task, payload={"unexpected": True}),
    )

    outcome = await handler(sessions, FakeTransferProvider())(invalid_context)
    task, file, run_count = load_state(sessions, task_id, file_id)

    assert outcome.status == "failed"
    assert outcome.error_code == "INVALID_TRANSFER_PAYLOAD"
    assert task.status == "pending"
    assert file.status == "pending"
    assert run_count == 0


async def test_rotated_provider_token_is_persisted(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id, file_id = seed_transfer(sessions)

    def persist_token(account: CloudAccount, _provider: object) -> bool:
        account.refresh_token = "rotated-encrypted-token"
        return True

    outcome = await handler(
        sessions,
        FakeTransferProvider(),
        token_persister=persist_token,
    )(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            file_id=file_id,
        )
    )

    with sessions() as session:
        account = session.scalar(select(CloudAccount))
        assert account is not None
        assert account.refresh_token == "rotated-encrypted-token"
    assert outcome.status == "success"
