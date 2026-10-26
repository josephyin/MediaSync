from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models import Base, CloudAccount, CloudFile, Subscription, Task, TaskRun
from app.services.download_service import DownloadTaskHandler
from app.task_engine.handlers import TaskHandlerRegistry, TaskOutcome
from app.task_engine.worker import WorkerRuntime

NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.fixture
def download_env(tmp_path, monkeypatch):
    import app.services.download_service as service
    import app.services.download_stream as stream

    engine = create_engine(f"sqlite:///{tmp_path / 'download.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as db:
        account = CloudAccount(provider="pikpak", name="test", refresh_token="encrypted")
        db.add(account)
        db.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            provider="pikpak",
            name="test",
            share_url="https://mypikpak.com/s/test",
            target_path="/",
        )
        db.add(subscription)
        db.flush()
        file = CloudFile(
            subscription_id=subscription.id,
            remote_file_id="source",
            filename="movie",
            relative_path="movie",
            fingerprint="test",
            status="saved",
            size=5,
            content_hash="6b4f89a54e2d27ecd7e8da05b4ab8fd9d1d8b119",
            target_file_id="saved",
            first_seen_at=NOW,
            last_seen_at=NOW,
        )
        db.add(file)
        db.flush()
        stat = tmp_path.stat()
        task = Task(
            type="download",
            file_id=file.id,
            subscription_id=subscription.id,
            account_id=account.id,
            payload={
                "root": str(tmp_path),
                "relative_path": "movie",
                "root_identity": [stat.st_dev, stat.st_ino],
            },
        )
        db.add(task)
        db.flush()
        task_id, file_id = task.id, file.id

    class Provider:
        async def get_download_url(self, _remote_id):
            return "https://dl.mypikpak.com/a"

        def consume_refresh_token_update(self):
            return None

    monkeypatch.setattr(service, "get_provider", lambda *_args: Provider())
    monkeypatch.setattr(service, "get_decrypted_token", lambda _account: "test")
    monkeypatch.setattr(get_settings(), "download_chunk_bytes", 3)
    original_client = httpx.AsyncClient

    def respond(request):
        start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
        return httpx.Response(
            206,
            headers={"Content-Range": f"bytes {start}-{end}/5"},
            content=b"hello"[start : end + 1],
        )

    monkeypatch.setattr(
        stream.httpx,
        "AsyncClient",
        lambda **_kwargs: original_client(transport=httpx.MockTransport(respond)),
    )
    yield sessions, task_id, file_id
    engine.dispose()


async def test_download_yields_to_scan_without_using_failure_budget(download_env, tmp_path):
    sessions, task_id, file_id = download_env
    with sessions.begin() as db:
        scan = Task(type="scan")
        db.add(scan)
        db.flush()
        scan_id = scan.id
    registry = TaskHandlerRegistry()
    registry.register("download", 1, DownloadTaskHandler(sessions))

    async def scan_handler(_context):
        return TaskOutcome(status="success", summary="scan completed")

    registry.register("scan", 1, scan_handler)
    clock = [NOW]
    worker = WorkerRuntime(
        session_factory=sessions, handlers=registry, worker_id="test", clock=lambda: clock[0]
    )
    first = await worker.run_once()
    assert first.task_id == task_id and first.task_status == "pending"
    assert not (tmp_path / "movie").exists()
    second = await worker.run_once()
    assert second.task_id == scan_id and second.task_status == "success"
    clock[0] += timedelta(seconds=2)
    final = await worker.run_once()
    assert final.task_id == task_id and final.task_status == "success"
    with sessions() as db:
        task = db.get(Task, task_id)
        assert task.retry_count == 0 and task.lock_token is None
        assert db.get(CloudFile, file_id).status == "saved"
        runs = list(
            db.scalars(
                select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.run_number)
            )
        )
        assert [x.status for x in runs] == ["success", "success"]
        assert runs[0].metrics["continuation"] is True
    assert (tmp_path / "movie").read_bytes() == b"hello"


async def test_replaced_mount_is_rejected_before_network_access(download_env):
    sessions, task_id, _file_id = download_env
    with sessions.begin() as db:
        task = db.get(Task, task_id)
        changed = Task(
            type="download",
            account_id=task.account_id,
            subscription_id=task.subscription_id,
            file_id=task.file_id,
            payload={**task.payload, "root_identity": [-1, -1]},
        )
        db.add(changed)
        db.flush()
        task_id = changed.id
    from app.services.download_service import execute_download

    outcome = await execute_download(task_id, sessions)
    assert outcome.status == "failed" and outcome.error_code == "DOWNLOAD_FAILED"
    assert "挂载已变化" in outcome.error_message


@pytest.mark.parametrize("new_login", [False, True])
@pytest.mark.parametrize("request_fails", [False, True])
async def test_token_rotation_cannot_overwrite_a_concurrent_login(
    download_env, monkeypatch, new_login, request_fails
):
    import app.services.download_service as service
    from app.core.security import get_credential_cipher
    from app.providers.pikpak.errors import PikPakRequestError
    from app.services.account_service import get_decrypted_token

    sessions, task_id, _file_id = download_env
    cipher = get_credential_cipher()
    with sessions() as db:
        account_id = db.get(Task, task_id).account_id
    new_credential = cipher.encrypt("new-login-token")

    class Provider:
        async def get_download_url(self, _remote_id):
            if request_fails:
                raise PikPakRequestError("PikPak 下载请求被拒绝")
            return "https://dl.mypikpak.com/a"

        def consume_refresh_token_update(self):
            return "rotated-old-token"

    class RacingCipher:
        def encrypt(self, value):
            # Reproduce a login occurring between credential read and rotation write.
            if new_login:
                with sessions.begin() as db:
                    db.get(CloudAccount, account_id).refresh_token = new_credential
            return cipher.encrypt(value)

    monkeypatch.setattr(service, "get_provider", lambda *_args: Provider())
    monkeypatch.setattr(service, "get_credential_cipher", RacingCipher)
    outcome = await service.execute_download(task_id, sessions)
    assert outcome.status == ("failed" if request_fails else "deferred")
    with sessions() as db:
        account = db.get(CloudAccount, account_id)
        assert get_decrypted_token(account) == (
            "new-login-token" if new_login else "rotated-old-token"
        )
