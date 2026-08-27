import argparse
import io
import json
from dataclasses import asdict

import httpx
import pytest

from app.providers.baidu import cli as baidu_cli
from app.providers.baidu.readonly_probe import (
    AccountProbeResult,
    BaiduAuthExpiredError,
    BaiduOpenReadOnlyProbe,
    BaiduUpstreamChangedError,
    ListingProbeResult,
    ReadOnlyProbeReport,
    normalize_access_token,
)


async def test_cli_can_read_token_from_standard_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    class FakeProbe:
        def __init__(self, token: str, *, timeout_seconds: float) -> None:
            received["token"] = token
            received["timeout_seconds"] = timeout_seconds

        async def __aenter__(self) -> "FakeProbe":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def run(self, **_kwargs: object) -> ReadOnlyProbeReport:
            return ReadOnlyProbeReport(
                account=AccountProbeResult(session_accepted=True),
                root=ListingProbeResult(item_count=0, total_count=None, field_names=()),
            )

    monkeypatch.setattr(baidu_cli.sys, "stdin", io.StringIO("token-value-long-enough\n"))
    monkeypatch.setattr(baidu_cli, "BaiduOpenReadOnlyProbe", FakeProbe)

    result = await baidu_cli._run(
        argparse.Namespace(
            token_stdin=True,
            token_clipboard=False,
            timeout=8.0,
            page_size=10,
        )
    )

    assert received == {"token": "token-value-long-enough\n", "timeout_seconds": 8.0}
    assert result["persisted"] is False


def test_cli_can_read_token_from_local_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = baidu_cli.subprocess.CompletedProcess(
        args=["/usr/bin/pbpaste"], returncode=0, stdout="token-value-long-enough\n"
    )
    monkeypatch.setattr(baidu_cli.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert baidu_cli._read_token_from_clipboard() == "token-value-long-enough\n"


@pytest.mark.parametrize(
    "token",
    ["", "short", "token with spaces long", "token-value-long\nInjected"],
)
def test_token_validation_rejects_unsafe_input(token: str) -> None:
    with pytest.raises(ValueError):
        normalize_access_token(token)


def test_token_validation_accepts_raw_or_bearer_token() -> None:
    assert normalize_access_token("token-value-long-enough") == "token-value-long-enough"
    assert normalize_access_token("Bearer token-value-long-enough") == "token-value-long-enough"


async def test_probe_uses_fixed_hosts_and_returns_only_structural_data() -> None:
    requests: list[httpx.Request] = []
    progress: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "pan.baidu.com"
        assert request.url.params["access_token"] == "token-value-long-enough"
        assert request.headers["User-Agent"] == "pan.baidu.com"
        if request.url.path == "/rest/2.0/xpan/nas":
            assert request.url.params["method"] == "uinfo"
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "uk": 123,
                    "baidu_name": "private-name",
                    "vip_type": 0,
                },
            )
        assert request.url.path == "/rest/2.0/xpan/file"
        assert request.url.params["method"] == "list"
        assert request.url.params["dir"] == "/"
        assert request.url.params["start"] == "0"
        assert request.url.params["limit"] == "10"
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "fs_id": 456,
                        "path": "/private-file.mkv",
                        "server_filename": "private-file.mkv",
                        "isdir": 0,
                        "size": 100,
                        "md5": "private-md5",
                        "untrusted": "must-not-be-reported",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduOpenReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        report = await probe.run(progress=progress.append)
    finally:
        await client.aclose()

    assert report.account.session_accepted is True
    assert report.root.item_count == 1
    assert report.root.total_count is None
    assert report.root.field_names == (
        "fs_id",
        "isdir",
        "md5",
        "path",
        "server_filename",
        "size",
    )
    assert progress == ["account", "root", "complete"]
    assert [request.url.path for request in requests] == [
        "/rest/2.0/xpan/nas",
        "/rest/2.0/xpan/file",
    ]
    serialized = json.dumps(asdict(report))
    for secret in (
        "private-name",
        "private-file.mkv",
        "private-md5",
        "token-value-long-enough",
        "untrusted",
    ):
        assert secret not in serialized


async def test_probe_classifies_auth_failure_without_echoing_token() -> None:
    token = "token-value-top-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errno": 111, "errmsg": token})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduOpenReadOnlyProbe(token, http_client=client)
    try:
        with pytest.raises(BaiduAuthExpiredError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert caught.value.code == "BAIDU_AUTH_EXPIRED"
    assert token not in str(caught.value)


async def test_probe_rejects_changed_account_shape() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errno": 0, "data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduOpenReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(BaiduUpstreamChangedError):
            await probe.probe_account()
    finally:
        await client.aclose()


async def test_probe_reports_invalid_json_without_echoing_body() -> None:
    secret_body = "<html>private gateway response</html>"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=secret_body,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduOpenReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(BaiduUpstreamChangedError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert str(caught.value) == (
        "Baidu Netdisk account returned invalid JSON "
        "(http_status=503, content_type=text/html)"
    )
    assert secret_body not in str(caught.value)

