from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.exceptions import ProviderRequestError
from app.main import app
from app.providers.baidu.qr_login import BaiduQrLogin

PNG_IMAGE = b"\x89PNG\r\n\x1a\n" + b"test-image"


async def test_baidu_qr_login_embeds_image_and_exchanges_bduss() -> None:
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path == "/v2/api/qrcode":
            return httpx.Response(200, content=PNG_IMAGE)
        if request.url.path == "/v2/api/getqrcode":
            assert request.url.params["lp"] == "pc"
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "sign": "a" * 32,
                    "imgurl": f"passport.baidu.com/v2/api/qrcode?sign={'a' * 32}&lp=pc",
                },
            )
        if request.url.path == "/channel/unicast":
            status_calls += 1
            assert request.url.params["channel_id"] == "a" * 32
            if status_calls == 1:
                return httpx.Response(200, text="")
            channel = json.dumps({"status": 0, "v": "temporary-bduss-value"})
            return httpx.Response(
                200,
                text=json.dumps({"errno": 0, "channel_v": channel}),
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    exchanged_values: list[str] = []

    def exchange(temp_bduss: str) -> str:
        exchanged_values.append(temp_bduss)
        return "BDUSS=final-bduss-cookie-value-long-enough"

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = BaiduQrLogin(
        base_url="https://passport.baidu.test",
        http_client=client,
        credential_exchanger=exchange,
    )
    try:
        started = await qr_login.start(account_name="百度家庭盘")
        waiting, _ = await qr_login.poll(started.session_id)
        confirmed, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert started.qr_code_data_url.startswith("data:image/png;base64,")
    assert waiting == "waiting"
    assert confirmed == "confirmed"
    assert session.account_name == "百度家庭盘"
    assert session.cookie == "BDUSS=final-bduss-cookie-value-long-enough"
    assert exchanged_values == ["temporary-bduss-value"]


async def test_baidu_qr_login_reports_scanned_without_temporary_bduss() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/api/qrcode":
            return httpx.Response(200, content=PNG_IMAGE)
        if request.url.path == "/v2/api/getqrcode":
            return httpx.Response(
                200,
                json={
                    "errno": 0,
                    "sign": "b" * 32,
                    "imgurl": f"https://passport.baidu.com/v2/api/qrcode?sign={'b' * 32}",
                },
            )
        channel = json.dumps({"status": 1, "v": ""})
        return httpx.Response(200, text=f'({json.dumps({"errno": 0, "channel_v": channel})})')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = BaiduQrLogin(
        base_url="https://passport.baidu.test",
        http_client=client,
        credential_exchanger=lambda _value: "BDUSS=unused-value-long-enough",
    )
    try:
        started = await qr_login.start(account_name="百度")
        status, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert status == "scanned"
    assert session.cookie is None


async def test_baidu_qr_login_rejects_untrusted_image_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "sign": "c" * 32,
                "imgurl": "https://example.com/qr.png",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = BaiduQrLogin(
        base_url="https://passport.baidu.test",
        http_client=client,
    )
    try:
        with pytest.raises(ProviderRequestError, match="invalid QR image URL"):
            await qr_login.start(account_name="百度")
    finally:
        await client.aclose()


def test_baidu_qr_login_api_creates_account_without_exposing_bduss(monkeypatch) -> None:
    from app.api.v1 import cloud_accounts as cloud_accounts_api

    class FakeQrLogin:
        async def start(self, *, account_id=None, account_name=None):
            assert account_id is None
            assert account_name == "百度扫码测试"
            return SimpleNamespace(
                session_id="baidu-session",
                qr_code_data_url="data:image/png;base64,iVBORw0KGgo=",
                expires_in=300,
            )

        async def poll(self, session_id):
            assert session_id == "baidu-session"
            return (
                "confirmed",
                SimpleNamespace(
                    account_id=None,
                    account_name="百度扫码测试",
                    cookie="BDUSS=private-bduss-cookie-value",
                ),
            )

        async def finish(self, session_id):
            assert session_id == "baidu-session"

    async def fake_verify_account(_db, account):
        account.account_identity = "百度扫码用户"
        account.provider_user_id = "baidu-user-id"
        return account

    monkeypatch.setattr(cloud_accounts_api, "baidu_qr_login", FakeQrLogin())
    monkeypatch.setattr(cloud_accounts_api, "verify_account", fake_verify_account)
    settings = get_settings()
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert login.status_code == 200
        started = client.post(
            "/api/v1/baidu/qr-login/start",
            json={"name": "百度扫码测试"},
        )
        assert started.status_code == 200
        completed = client.get("/api/v1/baidu/qr-login/baidu-session")

    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "confirmed"
    assert body["account"]["provider"] == "baidu"
    assert body["account"]["account_identity"] == "百度扫码用户"
    assert "refresh_token" not in body["account"]
    assert "private-bduss-cookie-value" not in str(body)
