import argparse
import io
import json
from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import pytest

from app.core.exceptions import ProviderWriteUncertainError
from app.providers.pan123 import cli as pan123_cli
from app.providers.pan123.readonly_probe import (
    AccountProbeResult,
    ListingProbeResult,
    Pan123AuthExpiredError,
    Pan123ReadOnlyProbe,
    Pan123ShareInvalidError,
    Pan123UpstreamChangedError,
    ReadOnlyProbeReport,
    build_request_signature,
    normalize_access_token,
    parse_share_url,
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
                root=ListingProbeResult(item_count=0, total_count=0, field_names=()),
            )

    monkeypatch.setattr(pan123_cli.sys, "stdin", io.StringIO("token-value-long-enough\n"))
    monkeypatch.setattr(pan123_cli, "Pan123ReadOnlyProbe", FakeProbe)

    result = await pan123_cli._run(
        argparse.Namespace(
            token_stdin=True,
            token_clipboard=False,
            check_share=False,
            timeout=8.0,
            page_size=10,
        )
    )

    assert received == {"token": "token-value-long-enough\n", "timeout_seconds": 8.0}
    assert result["persisted"] is False


def test_cli_can_read_token_from_local_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = pan123_cli.subprocess.CompletedProcess(
        args=["/usr/bin/pbpaste"], returncode=0, stdout="token-value-long-enough\n"
    )
    monkeypatch.setattr(pan123_cli.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert pan123_cli._read_token_from_clipboard() == "token-value-long-enough\n"


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


@pytest.mark.parametrize(
    "share_url",
    [
        "http://www.123pan.com/s/abcd",
        "https://evil.example/s/abcd",
        "https://www.123pan.com/not-share/abcd",
        "https://www.123pan.com/s/abc",
        "https://user@www.123pan.com/s/abcd",
    ],
)
def test_share_url_parser_rejects_unsafe_urls(share_url: str) -> None:
    with pytest.raises(ValueError):
        parse_share_url(share_url)


def test_share_url_parser_accepts_official_variants_and_password() -> None:
    assert parse_share_url("https://www.123pan.com/s/share_1234?pwd=8888") == (
        "share_1234",
        "8888",
    )
    assert parse_share_url("https://www.123865.com/s/share-1234") == (
        "share-1234",
        None,
    )
    assert parse_share_url(
        "https://1816063847.share.123pan.cn/123pan/share-1234?pwd=8888"
    ) == ("share-1234", "8888")
    assert parse_share_url("https://www.123pan.com/s/share-1234.html") == (
        "share-1234",
        None,
    )


@pytest.mark.parametrize(
    "share_url",
    [
        "https://abc.share.123pan.cn/123pan/share-1234",
        "https://1816063847.share.evil.example/123pan/share-1234",
        "https://share.123pan.cn/123pan/share-1234",
    ],
)
def test_share_url_parser_rejects_lookalike_dynamic_hosts(share_url: str) -> None:
    with pytest.raises(ValueError):
        parse_share_url(share_url)


def test_signature_is_deterministic_for_fixed_inputs() -> None:
    key, value = build_request_signature(
        "/b/api/user/info",
        now=datetime(2026, 8, 26, 4, 0, tzinfo=UTC),
        nonce=1234567,
    )
    assert key.isdecimal()
    assert value.startswith("1787716800-1234567-")
    assert value.rsplit("-", 1)[1].isdecimal()


async def test_probe_uses_fixed_hosts_and_returns_only_structural_data() -> None:
    requests: list[httpx.Request] = []
    progress: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "yun.123pan.com"
        assert request.headers["Authorization"] == "Bearer token-value-long-enough"
        assert request.headers["Platform"] == "web"
        signature_params = [
            value
            for key, value in request.url.params.multi_items()
            if key.isdecimal() and value.count("-") == 2
        ]
        assert len(signature_params) == 1
        if request.url.path == "/b/api/user/info":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"UID": 123, "Nickname": "private-name", "SpaceUsed": 10},
                },
            )
        if request.url.path == "/b/api/file/list/new":
            assert request.url.params["parentFileId"] == "0"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "InfoList": [
                            {
                                "FileId": 456,
                                "FileName": "private-file.mkv",
                                "Type": 0,
                                "Size": 100,
                                "secret": "must-not-be-reported",
                            }
                        ],
                        "Total": 12,
                    },
                },
            )
        assert request.url.path == "/b/api/share/get"
        assert request.url.params["shareKey"] == "share_1234"
        assert request.url.params["SharePwd"] == "8888"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "InfoList": [
                        {
                            "FileId": 789,
                            "FileName": "private-share-name",
                            "Type": 1,
                            "Etag": "private-etag",
                        }
                    ],
                    "Total": "1",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = Pan123ReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        report = await probe.run(
            share_url="https://www.123pan.com/s/share_1234",
            share_password="8888",
            progress=progress.append,
        )
    finally:
        await client.aclose()

    assert report.account.session_accepted is True
    assert report.root.item_count == 1
    assert report.root.total_count == 12
    assert report.root.field_names == ("FileId", "FileName", "Size", "Type")
    assert report.share is not None
    assert report.share.item_count == 1
    assert report.share.total_count == 1
    assert report.share.field_names == ("Etag", "FileId", "FileName", "Type")
    assert progress == ["account", "root", "share", "complete"]
    serialized = json.dumps(asdict(report))
    for secret in (
        "private-name",
        "private-file.mkv",
        "private-share-name",
        "private-etag",
        "token-value-long-enough",
        "8888",
    ):
        assert secret not in serialized


async def test_probe_classifies_auth_failure_without_echoing_token() -> None:
    token = "token-value-top-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "message": token})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = Pan123ReadOnlyProbe(token, http_client=client)
    try:
        with pytest.raises(Pan123AuthExpiredError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert caught.value.code == "PAN123_AUTH_EXPIRED"
    assert token not in str(caught.value)


async def test_probe_classifies_share_business_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/b/api/share/get"
        return httpx.Response(200, json={"code": 5103, "message": "password wrong"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = Pan123ReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(Pan123ShareInvalidError):
            await probe.probe_share("https://www.123pan.com/s/share_1234")
    finally:
        await client.aclose()


async def test_probe_rejects_changed_response_shape() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = Pan123ReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(Pan123UpstreamChangedError):
            await probe.probe_account()
    finally:
        await client.aclose()


async def test_probe_reports_invalid_json_stage_without_echoing_body() -> None:
    secret_body = "<html>private gateway response</html>"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            headers={"Content-Type": "text/html; charset=utf-8"},
            text=secret_body,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = Pan123ReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(Pan123UpstreamChangedError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert str(caught.value) == (
        "123 Cloud Drive account returned invalid JSON "
        "(http_status=503, content_type=text/html)"
    )
    assert secret_body not in str(caught.value)


async def test_write_response_with_changed_shape_is_uncertain() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = Pan123ReadOnlyProbe("token-value-long-enough", http_client=client)
    try:
        with pytest.raises(ProviderWriteUncertainError, match="do not submit again"):
            await probe._request(
                "share save",
                "https://yun.123pan.com",
                "/b/api/restful/goapi/v1/file/copy/save",
                method="POST",
                body={},
                write_may_be_accepted=True,
            )
    finally:
        await client.aclose()
