import argparse
import io
import json
from dataclasses import asdict

import httpx
import pytest

from app.providers.baidu import share_cli
from app.providers.baidu.share_probe import (
    AccountProbeResult,
    BaiduCookieExpiredError,
    BaiduShareInvalidError,
    BaiduShareReadOnlyProbe,
    ShareProbeResult,
    ShareReadOnlyProbeReport,
    normalize_cookie,
    parse_share_url,
)


async def test_cli_can_read_cookie_from_standard_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    class FakeProbe:
        def __init__(self, cookie: str, *, timeout_seconds: float) -> None:
            received["cookie"] = cookie
            received["timeout_seconds"] = timeout_seconds

        async def __aenter__(self) -> "FakeProbe":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def run(self, **_kwargs: object) -> ShareReadOnlyProbeReport:
            return ShareReadOnlyProbeReport(
                account=AccountProbeResult(session_accepted=True),
                share=ShareProbeResult(item_count=0, total_count=None, field_names=()),
            )

    monkeypatch.setattr(share_cli.sys, "stdin", io.StringIO("BDUSS=session-value\n"))
    monkeypatch.setattr(share_cli, "BaiduShareReadOnlyProbe", FakeProbe)
    monkeypatch.setattr(
        share_cli,
        "_prompt_share_details",
        lambda **_kwargs: ("https://pan.baidu.com/s/1share_key", "1234"),
    )

    result = await share_cli._run(
        argparse.Namespace(
            cookie_stdin=True,
            cookie_clipboard=False,
            timeout=8.0,
            page_size=10,
        )
    )

    assert received == {"cookie": "BDUSS=session-value\n", "timeout_seconds": 8.0}
    assert result["persisted"] is False


def test_cli_can_read_cookie_from_local_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = share_cli.subprocess.CompletedProcess(
        args=["/usr/bin/pbpaste"], returncode=0, stdout="BDUSS=session-value\n"
    )
    monkeypatch.setattr(share_cli.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert share_cli._read_cookie_from_clipboard() == "BDUSS=session-value\n"


@pytest.mark.parametrize(
    "cookie",
    [
        "",
        "missing-equals",
        "STOKEN=value",
        "BDUSS=value\nInjected: true",
    ],
)
def test_cookie_validation_rejects_unsafe_input(cookie: str) -> None:
    with pytest.raises(ValueError):
        normalize_cookie(cookie)


def test_cookie_validation_preserves_values_and_requires_bduss() -> None:
    assert normalize_cookie(" BDUSS=abc== ; STOKEN=rotating ") == "BDUSS=abc=="


def test_cookie_validation_accepts_a_bare_bduss_value() -> None:
    value = "a-valid-bduss-value-copied-from-browser"
    assert normalize_cookie(value) == f"BDUSS={value}"


def test_cookie_validation_accepts_bare_bduss_with_equals_padding() -> None:
    value = "a-valid-bduss-value-copied-from-browser=="
    assert normalize_cookie(value) == f"BDUSS={value}"


def test_cookie_validation_accepts_cookie_header_prefix() -> None:
    assert normalize_cookie("Cookie: BDUSS=session-value; STOKEN=other") == (
        "BDUSS=session-value"
    )


def test_cookie_validation_extracts_bduss_and_ignores_non_cookie_text() -> None:
    assert normalize_cookie(
        "Request Cookie: odd item; invalid name=value; BDUSS=session-value; Secure"
    ) == "BDUSS=session-value"


def test_cookie_validation_rejects_conflicting_bduss_values() -> None:
    with pytest.raises(ValueError, match="conflicting"):
        normalize_cookie("BDUSS=one; BDUSS=two")


@pytest.mark.parametrize(
    "share_url",
    [
        "http://pan.baidu.com/s/1abcd",
        "https://evil.example/s/1abcd",
        "https://pan.baidu.com/not-share/1abcd",
        "https://user@pan.baidu.com/s/1abcd",
        "https://pan.baidu.com/s/abc",
    ],
)
def test_share_url_parser_rejects_unsafe_urls(share_url: str) -> None:
    with pytest.raises(ValueError):
        parse_share_url(share_url)


def test_share_url_parser_accepts_password() -> None:
    assert parse_share_url("https://pan.baidu.com/s/1share_key?pwd=1234") == (
        "1share_key",
        "1234",
    )


async def test_probe_uses_fixed_hosts_and_returns_only_structural_data() -> None:
    requests: list[httpx.Request] = []
    progress: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "pan.baidu.com"
        assert request.headers["Cookie"] == "BDUSS=session-value"
        assert request.headers["User-Agent"] == "netdisk"
        if request.url.path == "/api/gettemplatevariable":
            assert dict(request.url.params) == {
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528",
                "clienttype": "0",
            }
            assert dict(httpx.QueryParams(request.content.decode())) == {
                "fields": '["uk","username","loginstate"]'
            }
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "result": {"uk": 123, "username": "private-name", "loginstate": 1},
                },
            )
        assert request.url.path == "/share/wxlist"
        assert request.url.params["channel"] == "weixin"
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form == {
            "pwd": "1234",
            "root": "1",
            "shorturl": "1share_key",
            "num": "10",
            "order": "time",
            "page": "1",
        }
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "data": {
                    "list": [
                        {
                            "fs_id": 456,
                            "path": "/private-share/private-file.mkv",
                            "server_filename": "private-file.mkv",
                            "isdir": 0,
                            "size": 100,
                            "md5": "private-md5",
                            "server_mtime": 1,
                            "secret": "must-not-be-reported",
                        }
                    ],
                    "uk": 999,
                    "shareid": 888,
                    "seckey": "private-seckey",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduShareReadOnlyProbe(
        "BDUSS=session-value; STOKEN=other-value",
        http_client=client,
    )
    try:
        report = await probe.run(
            share_url="https://pan.baidu.com/s/1share_key",
            password="1234",
            progress=progress.append,
        )
    finally:
        await client.aclose()

    assert report.account.session_accepted is True
    assert report.share.item_count == 1
    assert report.share.total_count is None
    assert report.share.field_names == (
        "fs_id",
        "isdir",
        "md5",
        "path",
        "server_filename",
        "server_mtime",
        "size",
    )
    assert progress == ["account", "share", "complete"]
    assert [request.url.path for request in requests] == [
        "/api/gettemplatevariable",
        "/share/wxlist",
    ]
    serialized = json.dumps(asdict(report))
    for secret in (
        "private-name",
        "private-file.mkv",
        "private-md5",
        "private-seckey",
        "session-value",
        "other-value",
        "1234",
    ):
        assert secret not in serialized


async def test_probe_classifies_expired_cookie_without_echoing_it() -> None:
    cookie = "BDUSS=top-secret-cookie"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errno": 111, "errmsg": cookie})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduShareReadOnlyProbe(cookie, http_client=client)
    try:
        with pytest.raises(BaiduCookieExpiredError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert caught.value.code == "BAIDU_COOKIE_EXPIRED"
    assert cookie not in str(caught.value)


async def test_probe_classifies_share_business_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errno": -9, "show_msg": "private"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = BaiduShareReadOnlyProbe("BDUSS=session-value", http_client=client)
    try:
        with pytest.raises(BaiduShareInvalidError):
            await probe.probe_share("https://pan.baidu.com/s/1share_key")
    finally:
        await client.aclose()
