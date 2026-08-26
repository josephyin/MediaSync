import argparse
import io
import json
from dataclasses import asdict

import httpx
import pytest

from app.providers.quark import cli as quark_cli
from app.providers.quark.readonly_probe import (
    AccountProbeResult,
    ListingProbeResult,
    QuarkAuthExpiredError,
    QuarkProbeError,
    QuarkReadOnlyProbe,
    QuarkShareInvalidError,
    QuarkUpstreamChangedError,
    QuarkWriteRejectedError,
    ReadOnlyProbeReport,
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

        async def run(self, **_kwargs: object) -> ReadOnlyProbeReport:
            return ReadOnlyProbeReport(
                account=AccountProbeResult(session_accepted=True),
                root=ListingProbeResult(item_count=0, total_count=0, field_names=()),
            )

    monkeypatch.setattr(quark_cli.sys, "stdin", io.StringIO("session=fake\n"))
    monkeypatch.setattr(quark_cli, "QuarkReadOnlyProbe", FakeProbe)

    result = await quark_cli._run(
        argparse.Namespace(
            cookie_stdin=True,
            cookie_clipboard=False,
            check_share=False,
            timeout=8.0,
            page_size=10,
        )
    )

    assert received == {"cookie": "session=fake\n", "timeout_seconds": 8.0}
    assert result["persisted"] is False


def test_cli_can_read_cookie_from_local_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = quark_cli.subprocess.CompletedProcess(
        args=["/usr/bin/pbpaste"], returncode=0, stdout="session=fake\n"
    )
    monkeypatch.setattr(quark_cli.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert quark_cli._read_cookie_from_clipboard() == "session=fake\n"


def test_cli_reads_share_details_from_terminal_when_cookie_uses_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TerminalContext:
        def __init__(self) -> None:
            self.input = io.StringIO("https://pan.quark.cn/s/share_1234\n")
            self.output = io.StringIO()

        def __enter__(self) -> "TerminalContext":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, value: str) -> int:
            return self.output.write(value)

        def flush(self) -> None:
            return None

        def readline(self) -> str:
            return self.input.readline()

    terminal = TerminalContext()
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: terminal)
    monkeypatch.setattr(
        quark_cli.getpass,
        "getpass",
        lambda _prompt, *, stream: "share-secret" if stream is terminal else "",
    )

    assert quark_cli._prompt_share_details(cookie_from_stdin=True) == (
        "https://pan.quark.cn/s/share_1234",
        "share-secret",
    )


@pytest.mark.parametrize(
    "cookie",
    [
        "",
        "missing-equals",
        "session=one; session=two",
        "session=one\nInjected: value",
        f"session={'x' * 16_385}",
    ],
)
def test_cookie_validation_rejects_unsafe_input(cookie: str) -> None:
    with pytest.raises(ValueError):
        normalize_cookie(cookie)


def test_cookie_validation_normalizes_without_changing_values() -> None:
    assert normalize_cookie(" session=abc== ; __puus=rotating ") == (
        "session=abc==; __puus=rotating"
    )


@pytest.mark.parametrize(
    "share_url",
    [
        "http://pan.quark.cn/s/abcd",
        "https://evil.example/s/abcd",
        "https://pan.quark.cn/not-share/abcd",
        "https://pan.quark.cn/s/abc",
        "https://pan.quark.cn/s/abcd?pwd=secret",
    ],
)
def test_share_url_parser_rejects_unsafe_or_ambiguous_urls(share_url: str) -> None:
    with pytest.raises(ValueError):
        parse_share_url(share_url)


def test_share_url_parser_returns_only_the_share_id() -> None:
    assert parse_share_url("https://pan.quark.cn/s/share_1234#/list/share") == "share_1234"


async def test_readonly_probe_uses_fixed_hosts_and_returns_only_structural_data() -> None:
    requests: list[httpx.Request] = []
    progress: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host in {"pan.quark.cn", "drive.quark.cn"}
        assert request.headers["Origin"] == "https://pan.quark.cn"
        if request.url.path == "/1/clouddrive/member":
            assert request.headers["Cookie"] == "session=fake; __puus=old"
            assert request.url.params["pr"] == "ucpro"
            assert request.url.params["fetch_identity"] == "false"
            return httpx.Response(
                200,
                headers=[
                    ("Set-Cookie", "__puus=new; Path=/; Secure; HttpOnly"),
                    ("Set-Cookie", "untrusted=value; Path=/"),
                ],
                json={
                    "status": 200,
                    "code": 0,
                    "data": {"member_type": "PRIVATE", "total_capacity": 1},
                },
            )
        assert request.headers["Cookie"] == "session=fake; __puus=new"
        if request.url.path == "/1/clouddrive/file/sort":
            assert request.url.params["pdir_fid"] == "0"
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "code": 0,
                    "data": {
                        "list": [
                            {
                                "fid": "private-fid",
                                "file_name": "private-name.mkv",
                                "file": True,
                                "size": 100,
                                "secret_extra": "must-not-be-reported",
                            }
                        ]
                    },
                    "metadata": {"_total": 23},
                },
            )
        if request.url.path == "/1/clouddrive/share/sharepage/token":
            assert json.loads(request.content) == {
                "pwd_id": "share_1234",
                "passcode": "test-password",
            }
            return httpx.Response(
                200,
                json={"status": 200, "code": 0, "data": {"stoken": "private-stoken"}},
            )
        assert request.url.path == "/1/clouddrive/share/sharepage/detail"
        assert request.url.params["stoken"] == "private-stoken"
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {
                    "list": [
                        {
                            "fid": "share-fid",
                            "file_name": "shared-private-name",
                            "dir": False,
                            "share_fid_token": "private-file-token",
                        }
                    ]
                },
                "metadata": {"_total": "1"},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = QuarkReadOnlyProbe("session=fake; __puus=old", http_client=client)
    try:
        report = await probe.run(
            share_url="https://pan.quark.cn/s/share_1234",
            share_password="test-password",
            progress=progress.append,
        )
    finally:
        await client.aclose()

    assert report.account.session_accepted is True
    assert report.root.item_count == 1
    assert report.root.total_count == 23
    assert report.root.field_names == ("fid", "file", "file_name", "size")
    assert report.share is not None
    assert report.share.item_count == 1
    assert report.share.total_count == 1
    assert report.share.field_names == ("dir", "fid", "file_name", "share_fid_token")
    assert report.rotated_cookie_names == ("__puus",)
    assert progress == ["account", "root", "share", "complete"]
    assert [request.url.path for request in requests] == [
        "/1/clouddrive/member",
        "/1/clouddrive/file/sort",
        "/1/clouddrive/share/sharepage/token",
        "/1/clouddrive/share/sharepage/detail",
    ]
    serialized = json.dumps(asdict(report))
    for secret in (
        "Private Name",
        "private-name.mkv",
        "private-fid",
        "private-stoken",
        "private-file-token",
        "test-password",
        "untrusted",
    ):
        assert secret not in serialized


async def test_probe_classifies_auth_failure_without_echoing_cookie() -> None:
    cookie = "session=top-secret-cookie"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"status": 401, "message": cookie})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = QuarkReadOnlyProbe(cookie, http_client=client)
    try:
        with pytest.raises(QuarkAuthExpiredError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert caught.value.code == "QUARK_AUTH_EXPIRED"
    assert "top-secret-cookie" not in str(caught.value)


async def test_probe_classifies_login_redirect_as_expired_auth() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://pan.quark.cn/login"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = QuarkReadOnlyProbe("session=fake", http_client=client)
    try:
        with pytest.raises(QuarkAuthExpiredError):
            await probe.probe_account()
    finally:
        await client.aclose()


async def test_probe_does_not_treat_unknown_string_code_as_success() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": 200, "code": "SESSION_INVALID", "data": {"nickname": "x"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = QuarkReadOnlyProbe("session=fake", http_client=client)
    try:
        with pytest.raises(QuarkProbeError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert getattr(caught.value, "code", None) == "QUARK_PROBE_FAILED"
    assert str(caught.value) == (
        "Quark rejected the account request "
        "(http_status=200, status=200, code=None)"
    )


async def test_probe_classifies_share_business_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/1/clouddrive/share/sharepage/token"
        return httpx.Response(
            200,
            json={"status": 200, "code": 41001, "message": "share unavailable"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = QuarkReadOnlyProbe("session=fake", http_client=client)
    try:
        with pytest.raises(QuarkShareInvalidError) as caught:
            await probe.probe_share("https://pan.quark.cn/s/share_1234")
    finally:
        await client.aclose()

    assert caught.value.code == "QUARK_SHARE_INVALID"


def test_probe_explains_same_account_share_save_rejection() -> None:
    with pytest.raises(QuarkWriteRejectedError) as caught:
        QuarkReadOnlyProbe._raise_for_error(
            "share save",
            404,
            {"status": 404, "code": 41017},
        )

    assert caught.value.code == "QUARK_WRITE_REJECTED"
    assert "another account" in str(caught.value)


async def test_probe_rejects_changed_response_shape() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 200, "code": 0, "data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = QuarkReadOnlyProbe("session=fake", http_client=client)
    try:
        with pytest.raises(QuarkUpstreamChangedError) as caught:
            await probe.probe_account()
    finally:
        await client.aclose()

    assert caught.value.code == "QUARK_UPSTREAM_CHANGED"
