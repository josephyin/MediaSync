from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CloudAccount,
    CloudFile,
    FolderCheckpoint,
    Subscription,
    Task,
)
from app.providers.base import (
    AccountProfile,
    FolderRef,
    RemoteItem,
    RemotePage,
    SaveResult,
    ShareInfo,
)
from app.services.scan_service import run_scan


class FakeProvider:
    def consume_refresh_token_update(self) -> str | None:
        return None

    async def validate_account(self) -> AccountProfile:
        return AccountProfile(identity="fake")

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        return ShareInfo(share_key="share-1", name="Share")

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        return RemotePage(
            items=[
                RemoteItem(
                    remote_file_id="file-1",
                    parent_id="root",
                    filename="movie.mkv",
                    item_type="file",
                    size=1024,
                )
            ]
        )

    async def resolve_target_path(self, path: str) -> FolderRef:
        return FolderRef("target", path)

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        return FolderRef(name, f"{parent.path}/{name}")

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        return None

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        return SaveResult("saved-1", f"{target.path}/{source.filename}")


async def test_repeated_scan_creates_one_file_and_transfer(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.scan_service.get_provider", lambda *_: FakeProvider())
    monkeypatch.setattr("app.services.scan_service.get_decrypted_token", lambda *_: "token")

    with Session(engine) as db:
        account = CloudAccount(
            provider="aliyundrive", name="test", refresh_token="encrypted", status="active"
        )
        db.add(account)
        db.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            name="test",
            provider="aliyundrive",
            share_url="https://www.alipan.com/s/share-1",
            share_key="share-1",
            source_folder_id="root",
            target_path="/Media",
            schedule="interval:30m",
            enabled=True,
            status="active",
            initial_sync_mode="all",
        )
        db.add(subscription)
        db.commit()

        first = await run_scan(db, subscription, "manual")
        second = await run_scan(db, subscription, "manual")

        assert first.status == "success"
        assert second.status == "success"
        assert len(list(db.scalars(select(CloudFile)))) == 1
        transfers = list(db.scalars(select(Task).where(Task.type == "transfer")))
        assert len(transfers) == 1
        assert transfers[0].status == "pending"


class TreeProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        self.calls.append(parent_id)
        if parent_id == "root":
            return RemotePage(
                items=[
                    RemoteItem(
                        remote_file_id=f"folder-{index}",
                        parent_id="root",
                        filename=f"Folder {index}",
                        item_type="folder",
                    )
                    for index in range(1, 4)
                ]
            )
        return RemotePage(
            items=[
                RemoteItem(
                    remote_file_id=f"file-{parent_id}",
                    parent_id=parent_id,
                    filename=f"{parent_id}.mkv",
                    item_type="file",
                    size=1024,
                )
            ]
        )


async def test_regular_scan_checks_root_and_oldest_folder_batch(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    provider = TreeProvider()
    monkeypatch.setattr("app.services.scan_service.get_provider", lambda *_: provider)
    monkeypatch.setattr("app.services.scan_service.get_decrypted_token", lambda *_: "token")
    monkeypatch.setattr(
        "app.services.scan_service.get_settings",
        lambda: SimpleNamespace(folder_scan_batch_size=1, full_scan_interval_hours=24),
    )

    with Session(engine) as db:
        account = CloudAccount(
            provider="aliyundrive", name="test", refresh_token="encrypted", status="active"
        )
        db.add(account)
        db.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            name="tree",
            provider="aliyundrive",
            share_url="https://www.alipan.com/s/share-1",
            source_folder_id="root",
            target_path="/Media",
            schedule="interval:30m",
            enabled=True,
            status="active",
            initial_sync_mode="all",
        )
        db.add(subscription)
        db.commit()

        first = await run_scan(db, subscription, "scheduled")
        assert first.status == "success"
        assert "完整校验完成" in (first.message or "")
        assert provider.calls == ["root", "folder-1", "folder-2", "folder-3"]
        assert len(list(db.scalars(select(FolderCheckpoint)))) == 3

        provider.calls.clear()
        second = await run_scan(db, subscription, "scheduled")

        assert second.status == "success"
        assert "增量轮询完成" in (second.message or "")
        assert provider.calls == ["root", "folder-1"]
