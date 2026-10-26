import asyncio
import fcntl

import httpx
import pytest

from app.services.download_stream import DownloadDeferred, DownloadError, stream_download

HELLO_GCID = "6b4f89a54e2d27ecd7e8da05b4ab8fd9d1d8b119"


async def no_progress(_count):
    pass


async def no_cancel():
    return False


def range_response(request, content=b"hello"):
    start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
    return httpx.Response(
        206,
        headers={"Content-Range": f"bytes {start}-{end}/{len(content)}"},
        content=content[start : end + 1],
    )


async def test_resume_uses_fresh_url_and_checks_gcid_before_publication(tmp_path):
    requests = []

    def respond(request):
        requests.append((request.headers["range"], request.url.params["token"]))
        return range_response(request)

    target = tmp_path / "movie"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DownloadDeferred):
            await stream_download(
                "https://dl.mypikpak.com/a?token=first",
                target,
                5,
                no_progress,
                no_cancel,
                client=client,
                chunk_bytes=3,
                identity="file-a",
                expected_hash=HELLO_GCID,
            )
        assert not target.exists()
        assert (tmp_path / ".movie.mediasync.part").read_bytes() == b"hel"
        await stream_download(
            "https://dl.mypikpak.com/a?token=second",
            target,
            5,
            no_progress,
            no_cancel,
            client=client,
            chunk_bytes=3,
            identity="file-a",
            expected_hash=HELLO_GCID,
        )
    assert requests == [("bytes=0-2", "first"), ("bytes=3-4", "second")]
    assert target.read_bytes() == b"hello"
    assert not list(tmp_path.glob("*.part*"))
    assert not list(tmp_path.glob(".*.part*"))


@pytest.mark.parametrize("status,content_range", [(206, "bytes 0-1/5"), (416, None)])
async def test_bad_range_does_not_append_or_publish(tmp_path, status, content_range):
    target = tmp_path / "movie"
    async with httpx.AsyncClient(transport=httpx.MockTransport(range_response)) as client:
        with pytest.raises(DownloadDeferred):
            await stream_download(
                "https://dl.mypikpak.com/a",
                target,
                5,
                no_progress,
                no_cancel,
                client=client,
                chunk_bytes=3,
            )
    response = httpx.Response(
        status, headers={"Content-Range": content_range} if content_range else {}, content=b"lo"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(DownloadError):
            await stream_download(
                "https://dl.mypikpak.com/a",
                target,
                5,
                no_progress,
                no_cancel,
                client=client,
                chunk_bytes=3,
            )
    assert not target.exists()
    assert (tmp_path / ".movie.mediasync.part").read_bytes() == b"hel"


async def test_ignored_range_restarts_without_appending(tmp_path):
    target = tmp_path / "movie"
    async with httpx.AsyncClient(transport=httpx.MockTransport(range_response)) as client:
        with pytest.raises(DownloadDeferred):
            await stream_download(
                "https://dl.mypikpak.com/a",
                target,
                5,
                no_progress,
                no_cancel,
                client=client,
                chunk_bytes=3,
            )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello"))
    ) as client:
        await stream_download(
            "https://dl.mypikpak.com/a", target, 5, no_progress, no_cancel, client=client
        )
    assert target.read_bytes() == b"hello"


async def test_changed_remote_identity_restarts_partial(tmp_path):
    requests = []

    def respond(request):
        requests.append(request.headers["range"])
        return range_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for identity in ("old-file", "new-file"):
            with pytest.raises(DownloadDeferred):
                await stream_download(
                    "https://dl.mypikpak.com/a",
                    tmp_path / "movie",
                    5,
                    no_progress,
                    no_cancel,
                    client=client,
                    chunk_bytes=3,
                    identity=identity,
                )
    assert requests == ["bytes=0-2", "bytes=0-2"]


async def test_restart_after_atomic_publication_finishes_without_overwrite(tmp_path, monkeypatch):
    import app.services.download_stream as stream

    target = tmp_path / "movie"
    real_link = stream.os.link

    def crash_after_link(source, destination):
        real_link(source, destination)
        raise OSError("simulated crash after publication")

    monkeypatch.setattr(stream.os, "link", crash_after_link)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello"))
    ) as client:
        with pytest.raises(OSError):
            await stream_download(
                "https://dl.mypikpak.com/a",
                target,
                5,
                no_progress,
                no_cancel,
                client=client,
                expected_hash=HELLO_GCID,
            )
        inode = target.stat().st_ino
        monkeypatch.setattr(stream.os, "link", real_link)
        assert (
            await stream_download(
                "https://dl.mypikpak.com/a",
                target,
                5,
                no_progress,
                no_cancel,
                client=client,
                expected_hash=HELLO_GCID,
            )
            == 5
        )
    assert target.read_bytes() == b"hello" and target.stat().st_ino == inode


async def test_unrelated_existing_file_is_never_overwritten(tmp_path):
    target = tmp_path / "movie"
    target.write_bytes(b"keep")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello"))
    ) as client:
        with pytest.raises(DownloadError, match="不会覆盖"):
            await stream_download(
                "https://dl.mypikpak.com/a", target, 5, no_progress, no_cancel, client=client
            )
    assert target.read_bytes() == b"keep"


async def test_gcid_detects_same_size_corruption(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"jello"))
    ) as client:
        with pytest.raises(DownloadError, match="GCID"):
            await stream_download(
                "https://dl.mypikpak.com/a",
                tmp_path / "movie",
                5,
                no_progress,
                no_cancel,
                client=client,
                expected_hash=HELLO_GCID,
            )
    assert not (tmp_path / "movie").exists()
    assert (tmp_path / ".movie.mediasync.part").stat().st_size == 0


async def test_verification_itself_yields_and_resumes(tmp_path):
    content = b"x" * 300000
    ranges = []

    def respond(request):
        ranges.append(request.headers["range"])
        return range_response(request, content)

    target = tmp_path / "movie"
    calls = 0
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for _ in range(10):
            calls += 1
            try:
                await stream_download(
                    "https://dl.mypikpak.com/a",
                    target,
                    len(content),
                    no_progress,
                    no_cancel,
                    client=client,
                    chunk_bytes=65536,
                    expected_hash="bdf0b4f7598915531e28c348845ae94cafe31c6d",
                )
                break
            except DownloadDeferred:
                assert not target.exists()
    assert len(ranges) == 5
    assert calls == 6  # Verification continues from its own checkpoint without network access.
    assert target.read_bytes() == content


async def test_second_writer_cannot_truncate_partial(tmp_path):
    partial = tmp_path / ".movie.mediasync.part"
    partial.write_bytes(b"keep")
    with partial.open("r+b") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with httpx.AsyncClient(transport=httpx.MockTransport(range_response)) as client:
            with pytest.raises(DownloadError, match="已有下载任务"):
                await stream_download(
                    "https://dl.mypikpak.com/a",
                    tmp_path / "movie",
                    5,
                    no_progress,
                    no_cancel,
                    client=client,
                )
    assert partial.read_bytes() == b"keep"


async def test_cancel_after_last_chunk_does_not_publish(tmp_path):
    cancelled = False

    async def progress(_count):
        nonlocal cancelled
        cancelled = True

    async def probe():
        return cancelled

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello"))
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await stream_download(
                "https://dl.mypikpak.com/a", tmp_path / "movie", 5, progress, probe, client=client
            )
    assert not (tmp_path / "movie").exists()


async def test_rate_limit_is_cooperative_and_cancellable(tmp_path, monkeypatch):
    import app.services.download_stream as stream

    slept = []

    async def sleep(delay):
        slept.append(delay)

    async def cancelled():
        return bool(slept)

    monkeypatch.setattr(stream.asyncio, "sleep", sleep)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"hello"))
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await stream_download(
                "https://dl.mypikpak.com/a",
                tmp_path / "movie",
                5,
                no_progress,
                cancelled,
                client=client,
                rate_limit=1,
            )
    assert 0 < slept[0] <= 0.25
    assert not (tmp_path / "movie").exists()
