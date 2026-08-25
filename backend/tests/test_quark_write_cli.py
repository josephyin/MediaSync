import subprocess
from pathlib import Path

import pytest

from app.core.exceptions import ProviderRequestError
from app.providers.base import FolderRef, RemoteItem, RemotePage, SaveOperation, ShareInfo
from app.providers.quark import write_cli


def test_clipboard_cookie_reader_uses_local_pbpaste(monkeypatch) -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/bin/pbpaste"], returncode=0, stdout="session=fake\n"
    )
    monkeypatch.setattr(write_cli.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert write_cli._read_cookie_from_clipboard() == "session=fake\n"


class FakeProvider:
    def __init__(self) -> None:
        self.start_calls = 0
        self.query_calls = 0
        self.target_calls = 0

    async def resolve_share(self, *_args):
        return ShareInfo("share-1", "Share", "0")

    async def list_share_items(self, *_args):
        return RemotePage([RemoteItem("source-1", "0", "movie.mkv", "file")])

    async def resolve_target_path(self, path):
        assert path == "/MediaSync测试"
        return FolderRef("target-1", path)

    async def find_target_item(self, *_args):
        self.target_calls += 1
        if self.target_calls == 1:
            return None
        return RemoteItem("saved-1", "target-1", "movie.mkv", "file")

    async def start_save_shared_item(self, *_args):
        self.start_calls += 1
        return "operation-1"

    async def query_save_operation(self, operation_id):
        self.query_calls += 1
        return SaveOperation(operation_id, True, ("saved-1",))

    def consume_refresh_token_update(self):
        return None


async def test_write_probe_persists_intent_then_verifies_and_removes_state(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    provider = FakeProvider()

    report = await write_cli.run_write_probe(
        provider,
        share_url="https://pan.quark.cn/s/share-1",
        share_password=None,
        target_path="/MediaSync测试",
        confirmed=True,
        poll_interval=0,
    )

    assert report.completed is True
    assert report.target_verified is True
    assert provider.start_calls == 1
    assert provider.query_calls == 1
    assert not state_path.exists()


async def test_uncertain_intent_is_never_resubmitted(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"status":"intent"}', encoding="utf-8")
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    provider = FakeProvider()

    with pytest.raises(ValueError, match="do not submit again"):
        await write_cli.run_write_probe(
            provider,
            share_url="https://pan.quark.cn/s/share-1",
            share_password=None,
            target_path="/MediaSync测试",
            confirmed=True,
            poll_interval=0,
        )

    assert provider.start_calls == 0


async def test_definitive_start_rejection_clears_intent_state(
    tmp_path: Path, monkeypatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    provider = FakeProvider()

    async def reject(*_args):
        raise ProviderRequestError("definitive rejection")

    provider.start_save_shared_item = reject
    with pytest.raises(ProviderRequestError, match="definitive rejection"):
        await write_cli.run_write_probe(
            provider,
            share_url="https://pan.quark.cn/s/share-1",
            share_password=None,
            target_path="/MediaSync测试",
            confirmed=True,
            poll_interval=0,
        )

    assert not state_path.exists()
