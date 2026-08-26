from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ProviderRequestError
from app.models import (
    Base,
    CloudAccount,
    CloudFile,
    FolderCheckpoint,
    Subscription,
    Task,
    TaskRun,
)
from app.providers.base import RemoteItem, RemotePage, ShareInfo
from app.task_engine.handlers import (
    TaskExecutionContext,
    TaskHandlerRegistry,
    TaskInvocation,
)
from app.task_engine.scan_handler import ScanTaskHandler
from app.task_engine.worker import WorkerRuntime

NOW = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "scan-handler.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


class FakeScanProvider:
    def __init__(
        self,
        pages: dict[tuple[str, str | None], RemotePage | Exception],
        *,
        resolve_error: Exception | None = None,
        page_delay: float = 0,
        after_page: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self.pages = pages
        self.resolve_error = resolve_error
        self.page_delay = page_delay
        self.after_page = after_page
        self.request_count = 0
        self.calls: list[tuple[str, str | None]] = []

    async def resolve_share(
        self,
        share_url: str,
        password: str | None = None,
    ) -> ShareInfo:
        if self.resolve_error is not None:
            raise self.resolve_error
        assert share_url == "https://www.alipan.com/s/share-1"
        assert password is None
        self.request_count += 1
        return ShareInfo(
            share_key="share-1",
            name="Share",
            root_folder_id="root",
        )

    async def list_share_items(
        self,
        _share: ShareInfo,
        parent_id: str,
        marker: str | None = None,
    ) -> RemotePage:
        self.calls.append((parent_id, marker))
        self.request_count += 1
        if self.page_delay:
            await asyncio.sleep(self.page_delay)
        page = self.pages[(parent_id, marker)]
        if isinstance(page, Exception):
            raise page
        if self.after_page is not None:
            self.after_page(parent_id, marker)
        return page


def file_item(
    remote_file_id: str,
    *,
    parent_id: str = "root",
    filename: str | None = None,
    size: int = 1024,
) -> RemoteItem:
    return RemoteItem(
        remote_file_id=remote_file_id,
        parent_id=parent_id,
        filename=filename or f"{remote_file_id}.mkv",
        item_type="file",
        size=size,
    )


def folder_item(remote_file_id: str, *, filename: str) -> RemoteItem:
    return RemoteItem(
        remote_file_id=remote_file_id,
        parent_id="root",
        filename=filename,
        item_type="folder",
    )


def seed_scan(
    sessions: sessionmaker[Session],
    *,
    initial_sync_mode: str = "all",
) -> tuple[int, int]:
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
            source_folder_id="root",
            target_path="/Media",
            schedule="interval:30m",
            enabled=True,
            status="active",
            initial_sync_mode=initial_sync_mode,
        )
        session.add(subscription)
        session.flush()
        task = Task(
            subscription_id=subscription.id,
            type="scan",
            status="pending",
            payload_version=1,
            payload={},
        )
        session.add(task)
        session.flush()
        return task.id, subscription.id


def context(
    *,
    task_id: int,
    subscription_id: int,
    payload: dict[str, object] | None = None,
    cancellation_probe: Callable[[], bool] | None = None,
) -> TaskExecutionContext:
    async def not_cancelled() -> bool:
        return False

    async def probe() -> bool:
        assert cancellation_probe is not None
        return cancellation_probe()

    return TaskExecutionContext(
        task=TaskInvocation(
            task_id=task_id,
            task_run_id=999,
            task_type="scan",
            payload_version=1,
            payload=payload or {},
            trigger_type="scheduled",
            account_id=None,
            subscription_id=subscription_id,
            file_id=None,
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
    provider: FakeScanProvider,
    *,
    token_persister=None,
) -> ScanTaskHandler:
    return ScanTaskHandler(
        session_factory=sessions,
        provider_factory=lambda *_args: provider,  # type: ignore[arg-type]
        token_loader=lambda _account: "refresh-token",
        token_persister=token_persister or (lambda *_args: False),
    )


def task_count(
    sessions: sessionmaker[Session],
    *,
    task_type: str,
) -> int:
    with sessions() as session:
        return (
            session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.type == task_type)
            )
            or 0
        )


async def test_full_scan_indexes_tree_and_enqueues_transfers_without_touching_scan_task(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(
                items=[
                    file_item("root-file"),
                    folder_item("season-1", filename="Season 1"),
                ]
            ),
            ("season-1", None): RemotePage(
                items=[
                    file_item(
                        "episode-1",
                        parent_id="season-1",
                        filename="episode-1.mkv",
                    )
                ]
            ),
        }
    )

    outcome = await handler(sessions, provider)(
        context(task_id=task_id, subscription_id=subscription_id)
    )

    with sessions() as session:
        scan_task = session.get(Task, task_id)
        subscription = session.get(Subscription, subscription_id)
        files = list(session.scalars(select(CloudFile).order_by(CloudFile.id)))
        checkpoints = list(session.scalars(select(FolderCheckpoint)))
        run_count = session.scalar(
            select(func.count()).select_from(TaskRun).where(TaskRun.task_id == task_id)
        )
        assert scan_task is not None
        assert subscription is not None
        assert scan_task.status == "pending"
        assert run_count == 0
        assert subscription.status == "active"
        assert subscription.last_scanned_at is not None
        assert subscription.last_full_scanned_at is not None
        assert [file.relative_path for file in files] == [
            "root-file.mkv",
            "Season 1",
            "Season 1/episode-1.mkv",
        ]
        assert len(checkpoints) == 1
    assert outcome.status == "success"
    assert outcome.metrics is not None
    assert outcome.metrics["full_scan"] is True
    assert outcome.metrics["folders_scanned"] == 2
    assert task_count(sessions, task_type="transfer") == 2


async def test_repeated_and_changed_files_preserve_transfer_idempotency(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(items=[file_item("movie", size=100)]),
        }
    )
    scan_handler = handler(sessions, provider)
    scan_context = context(task_id=task_id, subscription_id=subscription_id)

    first = await scan_handler(scan_context)
    second = await scan_handler(scan_context)
    provider.pages[("root", None)] = RemotePage(
        items=[file_item("movie", size=200)]
    )
    third = await scan_handler(scan_context)
    fourth = await scan_handler(scan_context)

    assert [result.status for result in (first, second, third, fourth)] == [
        "success",
        "success",
        "success",
        "success",
    ]
    assert second.metrics is not None
    assert second.metrics["full_scan"] is False
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CloudFile)) == 1
        transfer_keys = list(
            session.scalars(
                select(Task.idempotency_key)
                .where(Task.type == "transfer")
                .order_by(Task.id)
            )
        )
    assert len(transfer_keys) == 2
    assert transfer_keys[0] != transfer_keys[1]


async def test_future_only_initial_scan_indexes_without_transfer_intent(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(
        sessions,
        initial_sync_mode="future_only",
    )
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(items=[file_item("existing")]),
        }
    )

    outcome = await handler(sessions, provider)(
        context(task_id=task_id, subscription_id=subscription_id)
    )

    with sessions() as session:
        file = session.scalar(select(CloudFile))
        assert file is not None
        assert file.status == "discovered"
    assert outcome.status == "success"
    assert task_count(sessions, task_type="transfer") == 0


async def test_full_scan_prunes_stale_folder_checkpoints(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    with sessions() as session, session.begin():
        session.add(
            FolderCheckpoint(
                subscription_id=subscription_id,
                remote_folder_id="removed-folder",
                relative_path="Removed",
                last_seen_at=NOW - timedelta(days=2),
            )
        )
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(items=[]),
        }
    )

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            payload={"force_full": True},
        )
    )

    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(FolderCheckpoint)) == 0
    assert outcome.status == "success"
    assert outcome.metrics is not None
    assert outcome.metrics["full_scan"] is True


async def test_file_and_transfer_intent_roll_back_together_when_enqueue_fails(
    sessions: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(items=[file_item("atomic-file")]),
        }
    )

    def fail_create_task(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated enqueue failure")

    monkeypatch.setattr(
        "app.services.scan_service.TaskRepository.create_task",
        fail_create_task,
    )

    outcome = await handler(sessions, provider)(
        context(task_id=task_id, subscription_id=subscription_id)
    )

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        assert subscription is not None
        assert subscription.last_scanned_at is None
        assert session.scalar(select(func.count()).select_from(CloudFile)) == 0
    assert outcome.status == "retry"
    assert task_count(sessions, task_type="transfer") == 0


async def test_cancellation_between_pages_preserves_partial_data_without_completion(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    cancelled = False

    def cancel_after_first_page(_parent_id: str, marker: str | None) -> None:
        nonlocal cancelled
        if marker is None:
            cancelled = True

    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(
                items=[file_item("page-1")],
                next_marker="next",
            ),
            ("root", "next"): RemotePage(items=[file_item("page-2")]),
        },
        after_page=cancel_after_first_page,
    )

    outcome = await handler(sessions, provider)(
        context(
            task_id=task_id,
            subscription_id=subscription_id,
            cancellation_probe=lambda: cancelled,
        )
    )

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        files = list(session.scalars(select(CloudFile)))
        assert subscription is not None
        assert subscription.status == "active"
        assert subscription.last_error == "scan cancelled"
        assert subscription.last_scanned_at is None
        assert subscription.last_full_scanned_at is None
        assert [file.remote_file_id for file in files] == ["page-1"]
    assert outcome.status == "cancelled"
    assert task_count(sessions, task_type="transfer") == 1


async def test_partial_provider_failure_is_retryable_and_sanitized(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(
                items=[file_item("page-1")],
                next_marker="next",
            ),
            ("root", "next"): ProviderRequestError(
                "provider failed with secret-token"
            ),
        }
    )

    outcome = await handler(sessions, provider)(
        context(task_id=task_id, subscription_id=subscription_id)
    )

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        files = list(session.scalars(select(CloudFile)))
        assert subscription is not None
        assert subscription.status == "error"
        assert "secret-token" not in (subscription.last_error or "")
        assert subscription.last_scanned_at is None
        assert [file.remote_file_id for file in files] == ["page-1"]
    assert outcome.status == "retry"
    assert outcome.error_code == "PROVIDER_REQUEST_FAILED"
    assert "secret-token" not in (outcome.error_message or "")


async def test_invalid_credential_blocks_scan_without_consuming_provider_data(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {},
        resolve_error=ProviderRequestError(
            "InvalidParameter.RefreshToken: secret-token"
        ),
    )

    outcome = await handler(sessions, provider)(
        context(task_id=task_id, subscription_id=subscription_id)
    )

    with sessions() as session:
        subscription = session.get(Subscription, subscription_id)
        assert subscription is not None
        assert subscription.status == "error"
        assert subscription.last_error == "cloud-drive credential is invalid or expired"
    assert outcome.status == "waiting_credential"
    assert outcome.blocked_reason == "cloud-drive credential requires user action"


async def test_scan_payload_v1_validates_force_full_type(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    valid_context = context(task_id=task_id, subscription_id=subscription_id)
    invalid_context = replace(
        valid_context,
        task=replace(valid_context.task, payload={"force_full": "yes"}),
    )

    outcome = await handler(sessions, FakeScanProvider({}))(invalid_context)

    assert outcome.status == "failed"
    assert outcome.error_code == "INVALID_SCAN_PAYLOAD"


async def test_worker_heartbeat_survives_slow_scan_provider_call(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(items=[file_item("slow-file")]),
        },
        page_delay=1.25,
    )
    handlers = TaskHandlerRegistry()
    handlers.register("scan", 1, handler(sessions, provider))
    worker = WorkerRuntime(
        session_factory=sessions,
        handlers=handlers,
        worker_id="worker-a",
        lease_duration=timedelta(seconds=1),
        heartbeat_interval=timedelta(milliseconds=100),
        clock=lambda: datetime.now(UTC),
    )

    result = await worker.run_once()

    with sessions() as session:
        task = session.get(Task, task_id)
        run = session.scalar(select(TaskRun).where(TaskRun.task_id == task_id))
        subscription = session.get(Subscription, subscription_id)
        assert task is not None
        assert run is not None
        assert subscription is not None
        assert task.status == "success"
        assert run.status == "success"
        assert run.last_heartbeat_at is not None
        assert subscription.last_scanned_at is not None
    assert result.status == "completed"


async def test_rotated_provider_token_is_persisted(
    sessions: sessionmaker[Session],
) -> None:
    task_id, subscription_id = seed_scan(sessions)
    provider = FakeScanProvider(
        {
            ("root", None): RemotePage(items=[]),
        }
    )

    def persist_token(account: CloudAccount, _provider: object) -> bool:
        account.refresh_token = "rotated-encrypted-token"
        return True

    outcome = await handler(
        sessions,
        provider,
        token_persister=persist_token,
    )(
        context(task_id=task_id, subscription_id=subscription_id)
    )

    with sessions() as session:
        account = session.scalar(select(CloudAccount))
        assert account is not None
        assert account.refresh_token == "rotated-encrypted-token"
    assert outcome.status == "success"
