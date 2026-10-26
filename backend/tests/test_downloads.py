import asyncio

import httpx
import pytest

from app.services.download_service import (
    DownloadError,
    destination_path,
    stream_download,
    validate_download_url,
)


@pytest.mark.parametrize(
    "value",
    [
        "../x",
        "/tmp/x",
        "a/../../x",
        "a/./x",
        "a\\x",
        "",
        ".movie.mediasync.part",
        ".movie.mediasync.json",
        ".movie.mediasync.json.tmp",
    ],
)
def test_reject_unsafe_path(tmp_path, value):
    with pytest.raises(DownloadError):
        destination_path(tmp_path, value)


def test_reject_symlink_and_unmounted_root(tmp_path):
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(DownloadError):
        destination_path(tmp_path, "link/file")
    with pytest.raises(DownloadError):
        destination_path(tmp_path / "missing-mount", "file")
    assert destination_path(tmp_path, "movies/a.mkv") == tmp_path / "movies/a.mkv"


@pytest.mark.parametrize(
    "url",
    [
        "http://mypikpak.com/a",
        "https://127.0.0.1/a",
        "https://mypikpak.com.evil.test/a",
        "https://u:p@mypikpak.com/a",
    ],
)
def test_signed_url_host_boundary(url):
    with pytest.raises(DownloadError):
        validate_download_url(url)


async def no_cancel():
    return False


async def no_progress(count):
    pass


async def test_download_verified_then_published_no_overwrite(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"hello"))
    ) as client:
        target = tmp_path / "movie"
        assert (
            await stream_download(
                "https://dl.mypikpak.com/a", target, 5, no_progress, no_cancel, client=client
            )
            == 5
        )
        assert target.read_bytes() == b"hello"
        # A persisted completion receipt makes restart after atomic publication idempotent.
        assert (
            await stream_download(
                "https://dl.mypikpak.com/a", target, 5, no_progress, no_cancel, client=client
            )
            == 5
        )
        assert target.read_bytes() == b"hello"


async def test_partial_not_published_and_retry_restarts(tmp_path):
    content = b"bad"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=content))
    ) as client:
        target = tmp_path / "movie"
        with pytest.raises(DownloadError):
            await stream_download(
                "https://dl.mypikpak.com/a", target, 5, no_progress, no_cancel, client=client
            )
        assert not target.exists()
        content = b"hello"
        await stream_download(
            "https://dl.mypikpak.com/a", target, 5, no_progress, no_cancel, client=client
        )
        assert target.read_bytes() == b"hello"


async def test_redirect_to_localhost_rejected(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DownloadError):
            await stream_download(
                "https://dl.mypikpak.com/a",
                tmp_path / "movie",
                5,
                no_progress,
                no_cancel,
                client=client,
            )
    assert len(requests) == 1
    assert not (tmp_path / "movie").exists()


async def test_cancel_does_not_publish(tmp_path):
    async def cancelled():
        return True

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"hello"))
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await stream_download(
                "https://dl.mypikpak.com/a",
                tmp_path / "movie",
                5,
                no_progress,
                cancelled,
                client=client,
            )
        assert not (tmp_path / "movie").exists()


def test_download_api_requires_login_and_deduplicates(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.core.config import get_settings
    from app.core.database import get_db
    from app.core.security import require_admin
    from app.main import app
    from app.models import Base, CloudAccount, CloudFile, Subscription, Task
    from app.models.base import utcnow

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    def session():
        with Session(engine) as db:
            yield db

    with Session(engine) as db:
        account = CloudAccount(provider="pikpak", name="test", refresh_token="encrypted")
        db.add(account)
        db.flush()
        subscription = Subscription(
            cloud_account_id=account.id,
            provider="pikpak",
            name="test",
            share_url="https://mypikpak.com/s/test",
            target_path="/test",
        )
        db.add(subscription)
        db.flush()
        file = CloudFile(
            subscription_id=subscription.id,
            remote_file_id="source",
            filename="movie",
            relative_path="series/movie",
            fingerprint="hash",
            status="saved",
            target_file_id="saved",
            size=5,
            first_seen_at=utcnow(),
            last_seen_at=utcnow(),
        )
        db.add(file)
        db.commit()
        file_id = file.id
    monkeypatch.setattr(get_settings(), "download_root", str(tmp_path))
    monkeypatch.setattr(get_settings(), "download_settings_path", str(tmp_path / "settings.json"))
    app.dependency_overrides[get_db] = session
    try:
        with TestClient(app) as client:
            payload = {"file_ids": [file_id], "directory": "movies"}
            assert client.get("/api/v1/downloads/selection").status_code == 401
            assert client.post("/api/v1/downloads", json=payload).status_code == 401
            app.dependency_overrides[require_admin] = lambda: "admin"
            first = client.post("/api/v1/downloads", json=payload)
            assert first.status_code == 202
            assert client.post("/api/v1/downloads", json=payload).json() == first.json()
            assert (
                client.post(
                    "/api/v1/downloads", json={**payload, "directory": "../bad"}
                ).status_code
                == 400
            )
            task_id = first.json()["task_ids"][0]
            original_task_id = task_id
            other = tmp_path / "nas-mount"
            other.mkdir()
            assert client.get(
                "/api/v1/downloads/directories", params={"path": str(tmp_path)}
            ).json()["directories"] == [{"name": "nas-mount", "path": str(other)}]
            assert (
                client.put("/api/v1/downloads/config", json={"root": str(other)}).status_code == 200
            )
            assert client.get("/api/v1/downloads/config").json()["root"] == str(other)
            assert (
                client.put(
                    "/api/v1/downloads/config", json={"root": str(other / "missing")}
                ).status_code
                == 400
            )
            second = client.post("/api/v1/downloads", json=payload)
            assert second.status_code == 202
            assert second.json()["task_ids"] != first.json()["task_ids"]
            with Session(engine) as db:
                assert db.get(Task, original_task_id).payload["root"] == str(tmp_path)
                assert db.get(Task, second.json()["task_ids"][0]).payload["root"] == str(other)
            assert (
                client.post("/api/v1/downloads", json={**payload, "root": str(tmp_path)}).json()
                == first.json()
            )

            assert client.post(f"/api/v1/downloads/{task_id}/cancel").status_code == 202
            retry = client.post(f"/api/v1/downloads/{task_id}/retry")
            assert retry.status_code == 202
            successor_id = retry.json()["task_id"]
            assert successor_id != task_id
            assert (
                client.post(f"/api/v1/downloads/{task_id}/retry").json()["task_id"] == successor_id
            )
            with Session(engine) as db:
                assert db.get(CloudFile, file_id).status == "saved"
                assert db.get(Task, task_id).status == "cancelled"
                assert db.get(Task, successor_id).status == "pending"
                assert db.get(Task, task_id).completed_at is not None
            with Session(engine) as db:
                subscription_id = db.get(CloudFile, file_id).subscription_id
                for index in range(126):
                    db.add(
                        CloudFile(
                            subscription_id=subscription_id,
                            remote_file_id=f"bulk-{index}",
                            filename=f"bulk-{index}.txt",
                            relative_path=f"folder/bulk-{index}.txt",
                            fingerprint=f"bulk-{index}",
                            status="saved" if index < 125 else "pending",
                            target_file_id=f"target-{index}",
                            size=5,
                            first_seen_at=utcnow(),
                            last_seen_at=utcnow(),
                        )
                    )
                db.commit()
            selected = client.get(
                "/api/v1/downloads/selection",
                params={"query": "bulk-", "subscription_id": subscription_id},
            ).json()
            assert selected["count"] == 125
            assert file_id not in selected["file_ids"]
            assert (
                client.get("/api/v1/downloads/selection", params={"status": "failed"}).json()[
                    "count"
                ]
                == 0
            )
            assert (
                client.get("/api/v1/downloads/selection", params={"subscription_id": 99999}).json()[
                    "count"
                ]
                == 0
            )
            bulk_payload = {"file_ids": selected["file_ids"], "directory": "bulk-test"}
            queued = client.post("/api/v1/downloads", json=bulk_payload)
            assert queued.status_code == 202
            assert len(queued.json()["task_ids"]) == 125
            assert client.post("/api/v1/downloads", json=bulk_payload).json() == queued.json()
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)
        engine.dispose()
