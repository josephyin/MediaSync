from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.exceptions import ProviderRequestError
from app.main import app
from app.providers.pan123.qr_login import Pan123QrLogin


async def test_pan123_qr_login_generates_local_svg_and_exchanges_token() -> None:
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path == "/centerlogin":
            return httpx.Response(200, text="login")
        assert request.headers["loginuuid"]
        if request.url.path == "/api/user/qr-code/generate":
            assert request.method == "GET"
            assert request.url.params["uniID"]
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uniID": "server-session-id",
                        "url": "https://yun.123pan.cn/wx-app-login.html",
                    },
                },
            )
        if request.url.path == "/api/user/qr-code/result":
            status_calls += 1
            assert request.url.params["uniID"] == "server-session-id"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"loginStatus": 0 if status_calls == 1 else 1},
                },
            )
        if request.url.path == "/api/user/qr-code/wx_code":
            assert request.method == "POST"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"wxCode": "wechat-login-code"}},
            )
        if request.url.path == "/api/user/sign_in":
            assert request.method == "POST"
            assert b'"type":4' in request.content
            return httpx.Response(
                200,
                json={"code": 0, "data": {"token": "pan123-access-token-value"}},
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = Pan123QrLogin(
        base_url="https://user.123pan.test",
        http_client=client,
    )
    try:
        started = await qr_login.start(account_name="家庭 123 云盘")
        waiting, _ = await qr_login.poll(started.session_id)
        confirmed, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert started.qr_code_data_url.startswith("data:image/svg+xml;base64,")
    assert waiting == "waiting"
    assert confirmed == "confirmed"
    assert session.account_name == "家庭 123 云盘"
    assert session.access_token == "pan123-access-token-value"


async def test_pan123_qr_login_keeps_scanned_state_until_wechat_code_is_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/centerlogin":
            return httpx.Response(200)
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uniID": "pending-session",
                        "url": "https://yun.123pan.cn/wx-app-login.html",
                    },
                },
            )
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json={"code": 0, "data": {"loginStatus": 1}})
        if request.url.path.endswith("/wx_code"):
            return httpx.Response(200, json={"code": 0, "data": {"wxCode": ""}})
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = Pan123QrLogin(
        base_url="https://user.123pan.test",
        http_client=client,
    )
    try:
        started = await qr_login.start(account_name="123")
        status, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert status == "scanned"
    assert session.access_token is None


async def test_pan123_qr_login_recovers_authorization_code_at_expiry_boundary() -> None:
    result_calls = 0
    code_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal result_calls, code_calls
        if request.url.path == "/centerlogin":
            return httpx.Response(200)
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uniID": "expiry-boundary",
                        "url": "https://yun.123pan.cn/wx-app-login.html",
                    },
                },
            )
        if request.url.path.endswith("/result"):
            result_calls += 1
            return httpx.Response(
                200,
                json={"code": 0, "data": {"loginStatus": 1 if result_calls == 1 else 3}},
            )
        if request.url.path.endswith("/wx_code"):
            code_calls += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"wxCode": "" if code_calls == 1 else "last-chance-code"},
                },
            )
        if request.url.path.endswith("/sign_in"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"token": "expiry-boundary-access-token"}},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = Pan123QrLogin(
        base_url="https://user.123pan.test",
        http_client=client,
    )
    try:
        started = await qr_login.start(account_name="123")
        scanned, _ = await qr_login.poll(started.session_id)
        confirmed, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert scanned == "scanned"
    assert confirmed == "confirmed"
    assert session.access_token == "expiry-boundary-access-token"


async def test_pan123_qr_login_rejects_untrusted_qr_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/centerlogin":
            return httpx.Response(200)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "uniID": "unsafe-session",
                    "url": "https://example.com/login",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = Pan123QrLogin(
        base_url="https://user.123pan.test",
        http_client=client,
    )
    try:
        with pytest.raises(ProviderRequestError, match="invalid QR login URL"):
            await qr_login.start(account_name="123")
    finally:
        await client.aclose()


def test_pan123_qr_login_api_creates_account_without_exposing_token(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.api.v1 import cloud_accounts as cloud_accounts_api

    class FakeQrLogin:
        async def start(self, *, account_id=None, account_name=None):
            assert account_id is None
            assert account_name == "扫码测试账号"
            return SimpleNamespace(
                session_id="pan123-session",
                qr_code_data_url="data:image/svg+xml;base64,PHN2Zy8+",
                expires_in=120,
            )

        async def poll(self, session_id):
            assert session_id == "pan123-session"
            return (
                "confirmed",
                SimpleNamespace(
                    account_id=None,
                    account_name="扫码测试账号",
                    access_token="pan123-secret-access-token",
                ),
            )

        async def finish(self, session_id):
            assert session_id == "pan123-session"

    async def fake_verify_account(_db, account):
        account.account_identity = "扫码用户"
        account.provider_user_id = "123456"
        account.default_drive_id = "0"
        return account

    monkeypatch.setattr(cloud_accounts_api, "pan123_qr_login", FakeQrLogin())
    monkeypatch.setattr(cloud_accounts_api, "verify_account", fake_verify_account)
    settings = get_settings()
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert login.status_code == 200
        started = client.post(
            "/api/v1/pan123/qr-login/start",
            json={"name": "扫码测试账号"},
        )
        assert started.status_code == 200
        assert started.json()["session_id"] == "pan123-session"
        completed = client.get("/api/v1/pan123/qr-login/pan123-session")

    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "confirmed"
    assert body["account"]["provider"] == "pan123"
    assert body["account"]["account_identity"] == "扫码用户"
    assert "refresh_token" not in body["account"]
    assert "pan123-secret-access-token" not in str(body)
