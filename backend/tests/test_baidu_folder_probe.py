from pathlib import Path

import httpx
import pytest

from app.core.exceptions import ProviderRequestError, ProviderWriteUncertainError
from app.providers.baidu import folder_cli
from app.providers.baidu.open_write_client import (
    BaiduFolderWriteRejectedError,
    BaiduOpenWriteClient,
)


class FakeClient:
    def __init__(self) -> None:
        self.create_calls = 0
        self.list_calls = 0

    async def probe_account(self):
        return None

    async def fetch_directory(self, *_args, **_kwargs):
        self.list_calls += 1
        items = [] if self.list_calls == 1 else [
            {"server_filename": "AutoFolderProbe", "isdir": 1}
        ]
        return {"errno": 0, "list": items}

    async def create_folder(self, *_args, **_kwargs):
        self.create_calls += 1


async def test_folder_probe_persists_intent_then_verifies_and_removes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(folder_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    report = await folder_cli.run_folder_probe(
        client,
        target_path="/MediaSync-Write-Probe/AutoFolderProbe",
        confirmed=True,
        poll_interval=0,
    )

    assert report.completed is True
    assert report.target_verified is True
    assert client.create_calls == 1
    assert not state_path.exists()


async def test_uncertain_folder_intent_is_never_resubmitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"status":"intent"}', encoding="utf-8")
    monkeypatch.setattr(folder_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    with pytest.raises(ProviderWriteUncertainError, match="do not submit again"):
        await folder_cli.run_folder_probe(
            client,
            target_path="/MediaSync-Write-Probe/AutoFolderProbe",
            confirmed=True,
            poll_interval=0,
        )

    assert client.create_calls == 0


async def test_definitive_folder_rejection_clears_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(folder_cli, "_state_path", lambda *_args: state_path)
    client = FakeClient()

    async def reject(*_args, **_kwargs):
        raise ProviderRequestError("definitive rejection")

    client.create_folder = reject
    with pytest.raises(ProviderRequestError, match="definitive rejection"):
        await folder_cli.run_folder_probe(
            client,
            target_path="/MediaSync-Write-Probe/AutoFolderProbe",
            confirmed=True,
            poll_interval=0,
        )

    assert not state_path.exists()


async def test_open_write_client_submits_create_once_and_rejects_business_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"errno": -10, "show_msg": "private"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduOpenWriteClient("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(BaiduFolderWriteRejectedError) as caught:
            await probe.create_folder("/MediaSync-Write-Probe/AutoFolderProbe")
    finally:
        await client.aclose()

    assert caught.value.code == "BAIDU_WRITE_REJECTED"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/rest/2.0/xpan/file"
    assert request.url.params["method"] == "create"
    assert request.url.params["access_token"] == "token-value-long-enough"
    form = dict(httpx.QueryParams(request.content.decode()))
    assert form == {
        "path": "/MediaSync-Write-Probe/AutoFolderProbe",
        "size": "0",
        "isdir": "1",
        "rtype": "3",
    }


async def test_folder_create_transport_failure_is_uncertain_and_not_retried() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("uncertain")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduOpenWriteClient("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(ProviderWriteUncertainError, match="do not submit again"):
            await probe.create_folder("/MediaSync-Write-Probe/AutoFolderProbe")
    finally:
        await client.aclose()

    assert attempts == 1

