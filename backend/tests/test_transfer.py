from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, CloudAccount, CloudFile, Subscription, Task
from app.models.base import utcnow
from app.providers.base import FolderRef
from app.services.transfer_service import _target_folder, run_transfer


class FolderProvider:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        self.created.append((parent.folder_id, name))
        path = f"{parent.path.rstrip('/')}/{name}"
        return FolderRef(folder_id=f"folder-{name}", path=path)


async def test_target_folder_creates_root_and_relative_directories() -> None:
    provider = FolderProvider()

    target = await _target_folder(provider, "/Media", "Movies/2026/movie.mkv")

    assert provider.created == [
        ("root", "Media"),
        ("folder-Media", "Movies"),
        ("folder-Movies", "2026"),
    ]
    assert target == FolderRef("folder-2026", "/Media/Movies/2026")


class FailingProvider:
    async def resolve_share(self, *_args: object) -> object:
        raise RuntimeError("temporary provider failure")


async def test_transfer_failure_is_deferred_before_next_attempt(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr("app.services.transfer_service.get_provider", lambda *_: FailingProvider())
    monkeypatch.setattr("app.services.transfer_service.get_decrypted_token", lambda *_: "token")
    monkeypatch.setattr("app.services.transfer_service.persist_provider_token", lambda *_: False)
    monkeypatch.setattr("app.services.transfer_service.random.randint", lambda *_: 0)

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
            target_path="/Media",
            schedule="interval:30m",
        )
        db.add(subscription)
        db.flush()
        now = utcnow()
        file = CloudFile(
            subscription_id=subscription.id,
            remote_file_id="file-1",
            filename="movie.mkv",
            relative_path="movie.mkv",
            item_type="file",
            fingerprint="file-1:1024",
            status="pending",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(file)
        db.flush()
        task = Task(
            subscription_id=subscription.id,
            file_id=file.id,
            type="transfer",
            status="pending",
            max_attempts=3,
        )
        db.add(task)
        db.commit()

        before = utcnow()
        await run_transfer(db, task)

        assert task.status == "pending"
        assert task.attempt_count == 1
        assert task.next_attempt_at is not None
        assert task.next_attempt_at.replace(tzinfo=before.tzinfo) > before
        assert file.status == "pending"
        assert file.last_error == "temporary provider failure"
