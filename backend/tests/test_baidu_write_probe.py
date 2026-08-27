from pathlib import Path

import httpx
import pytest

from app.core.exceptions import ProviderRequestError, ProviderWriteUncertainError
from app.providers.baidu import write_cli
from app.providers.baidu.write_client import (
    BaiduWriteClient,
    BaiduWriteRejectedError,
    decode_sekey,
    normalize_target_path,
)


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
                    "shareid": 11,
                    "uk": 22,
                    "seckey": "secret",
                    "list": [
                        {
                            "fs_id": 10,
                            "server_filename": "probe.txt",
                            "size": 5,
                        }
                    ],
                }
            },
        )

    async def fetch_target_page(self, *_args, **_kwargs):
        self.target_calls += 1
        return {"errno": 0, "list": []}

    async def save_share_item(self, **_kwargs):
        self.save_calls += 1

    async def wait_for_target_item(self, *_args, **_kwargs):
        return True


@pytest.mark.parametrize("path", ["relative", "/bad/../path", "/bad//path", ""])
def test_target_path_rejects_unsafe_values(path: str) -> None:
    with pytest.raises(ValueError):
        normalize_target_path(path)


def test_sekey_decoding_matches_baidu_share_encoding() -> None:
    assert decode_sekey("abc-def_ghi~") == "abc+def/ghi="


async def test_write_probe_persists_intent_then_verifies_and_removes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(write_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    report = await write_cli.run_write_probe(
        client,
        share_url="https://pan.baidu.com/s/1share-key",
        share_password="",
        target_path="/MediaSync-Write-Probe",
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
            share_url="https://pan.baidu.com/s/1share-key",
            share_password="",
            target_path="/MediaSync-Write-Probe",
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
            share_url="https://pan.baidu.com/s/1share-key",
            share_password="",
            target_path="/MediaSync-Write-Probe",
            confirmed=True,
            poll_interval=0,
        )

    assert not state_path.exists()


async def test_write_client_submits_once_and_rejects_business_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errno": 12, "show_msg": "private"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduWriteClient("BDUSS=session-value", http_client=client)
    try:
        with pytest.raises(BaiduWriteRejectedError) as caught:
            await probe.save_share_item(
                share_url="https://pan.baidu.com/s/1share-key",
                share_data={"shareid": 11, "uk": 22, "seckey": "abc-def~"},
                source={"fs_id": 10},
                target_path="/MediaSync-Write-Probe",
            )
    finally:
        await client.aclose()

    assert caught.value.code == "BAIDU_WRITE_REJECTED"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/share/transfer"
    assert request.url.params["shareid"] == "11"
    assert request.url.params["from"] == "22"
    assert request.url.params["sekey"] == "abc+def="
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form["path"] == "/MediaSync-Write-Probe"
    assert form["fsidlist"] == "[10]"


async def test_write_transport_failure_is_uncertain_and_not_retried() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("uncertain")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduWriteClient("BDUSS=session-value", http_client=client)
    try:
        with pytest.raises(ProviderWriteUncertainError, match="do not submit again"):
            await probe.save_share_item(
                share_url="https://pan.baidu.com/s/1share-key",
                share_data={"shareid": 11, "uk": 22, "seckey": "secret"},
                source={"fs_id": 10},
                target_path="/MediaSync-Write-Probe",
            )
    finally:
        await client.aclose()

    assert attempts == 1

