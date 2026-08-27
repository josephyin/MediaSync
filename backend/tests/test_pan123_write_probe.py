from pathlib import Path

import pytest

from app.core.exceptions import ProviderRequestError, ProviderWriteUncertainError
from app.providers.pan123 import write_cli


class FakeClient:
    def __init__(self) -> None:
        self.save_calls = 0
        self.target_calls = 0

    async def probe_account(self):
        return None

    async def fetch_share_page(self, *_args, **_kwargs):
        return (
            "share-key",
            "",
            {
                "data": {
                    "InfoList": [
                        {
                            "FileId": 10,
                            "FileName": "probe.txt",
                            "Size": 5,
                            "Type": 0,
                            "Etag": "etag",
                        }
                    ]
                }
            },
        )

    async def fetch_drive_page(self, *_args, **_kwargs):
        self.target_calls += 1
        items = [] if self.target_calls == 1 else [{"FileName": "probe.txt"}]
        return {"data": {"InfoList": items}}

    async def save_share_item(self, **_kwargs):
        self.save_calls += 1


async def test_write_probe_persists_intent_then_verifies_and_removes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    report = await write_cli.run_write_probe(
        client,
        share_url="https://www.123pan.com/s/share-key",
        share_password="",
        target_folder_id="0",
        login_uuid="1234567890abcdef",
        login_uuid_generated=True,
        confirmed=True,
        poll_interval=0,
    )

    assert report.completed is True
    assert report.target_verified is True
    assert client.save_calls == 1
    assert not state_path.exists()


async def test_uncertain_intent_is_never_resubmitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"status":"intent"}', encoding="utf-8")
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    with pytest.raises(ProviderWriteUncertainError, match="do not submit again"):
        await write_cli.run_write_probe(
            client,
            share_url="https://www.123pan.com/s/share-key",
            share_password="",
            target_folder_id="0",
            login_uuid="1234567890abcdef",
            login_uuid_generated=True,
            confirmed=True,
            poll_interval=0,
        )

    assert client.save_calls == 0


async def test_definitive_rejection_clears_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    async def reject(**_kwargs):
        raise ProviderRequestError("definitive rejection")

    client.save_share_item = reject
    with pytest.raises(ProviderRequestError, match="definitive rejection"):
        await write_cli.run_write_probe(
            client,
            share_url="https://www.123pan.com/s/share-key",
            share_password="",
            target_folder_id="0",
            login_uuid="1234567890abcdef",
            login_uuid_generated=True,
            confirmed=True,
            poll_interval=0,
        )

    assert not state_path.exists()
