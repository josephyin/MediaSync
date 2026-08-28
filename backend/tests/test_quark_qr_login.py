from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.exceptions import ProviderRequestError
from app.main import app
from app.providers.quark.qr_login import QuarkQrLogin


async def test_quark_qr_login_generates_local_svg_and_exchanges_cookie() -> None:
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        assert request.url.host in {"uop.quark.cn", "pan.quark.cn"}
        if request.url.path.endswith("getTokenForQrcodeLogin"):
            assert request.url.params["client_id"] == "532"
            assert request.url.params["request_id"]
            return httpx.Response(
                200,
                json={
                    "status": 2_000_000,
                    "message": "ok",
                    "data": {"members": {"token": "private-qr-token"}},
                },
            )
        if request.url.path.endswith("getServiceTicketByQrcodeToken"):
            status_calls += 1
            assert request.url.params["token"] == "private-qr-token"
            if status_calls == 1:
                return httpx.Response(200, json={"status": 50_004_001})
            return httpx.Response(
                200,
                json={
                    "status": 2_000_000,
                    "message": "ok",
                    "data": {"members": {"service_ticket": "service-ticket"}},
                },
            )
        if request.url.path == "/account/info":
            assert request.url.params["st"] == "service-ticket"
            assert request.url.params["lw"] == "scan"
            return httpx.Response(
                200,
                json={"status": 200, "data": {"nickname": "扫码用户"}},
                headers=[
                    ("set-cookie", "__pus=pus-value; Domain=.quark.cn; Path=/; Secure"),
                    ("set-cookie", "__kps=kps-value; Domain=.quark.cn; Path=/; Secure"),
                    ("set-cookie", "__uid=uid-value; Domain=.quark.cn; Path=/; Secure"),
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = QuarkQrLogin(http_client=client)
    try:
        started = await qr_login.start(account_name="家庭夸克")
        waiting, _ = await qr_login.poll(started.session_id)
        confirmed, session = await qr_login.poll(started.session_id)
    finally:
        await client.aclose()

    assert started.qr_code_data_url.startswith("data:image/svg+xml;base64,")
    assert "private-qr-token" not in started.qr_code_data_url
    assert waiting == "waiting"
    assert confirmed == "confirmed"
    assert session.account_name == "家庭夸克"
    assert session.cookie == "__pus=pus-value; __kps=kps-value; __uid=uid-value"


async def test_quark_qr_login_rejects_incomplete_cookie() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getTokenForQrcodeLogin"):
            return httpx.Response(
                200,
                json={
                    "status": 2_000_000,
                    "data": {"members": {"token": "qr-token"}},
                },
            )
        if request.url.path.endswith("getServiceTicketByQrcodeToken"):
            return httpx.Response(
                200,
                json={
                    "status": 2_000_000,
                    "data": {"members": {"service_ticket": "ticket"}},
                },
            )
        return httpx.Response(
            200,
            headers={"set-cookie": "__uid=uid-value; Domain=.quark.cn; Path=/; Secure"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    qr_login = QuarkQrLogin(http_client=client)
    try:
        started = await qr_login.start(account_name="夸克")
        with pytest.raises(ProviderRequestError, match="incomplete Cookie"):
            await qr_login.poll(started.session_id)
    finally:
        await client.aclose()


def test_quark_qr_login_rejects_non_official_origins() -> None:
    with pytest.raises(ValueError, match="official HTTPS hostname"):
        QuarkQrLogin(uop_origin="http://uop.quark.cn")
    with pytest.raises(ValueError, match="official HTTPS hostname"):
        QuarkQrLogin(pan_origin="https://pan.quark.cn/account/info")
    with pytest.raises(ValueError, match="official HTTPS hostname"):
        QuarkQrLogin(uop_origin="https://example.com")


def test_quark_qr_login_api_creates_account_without_exposing_cookie(monkeypatch) -> None:
    from app.api.v1 import cloud_accounts as cloud_accounts_api

    class FakeQrLogin:
        async def start(self, *, account_id=None, account_name=None):
            assert account_id is None
            assert account_name == "扫码夸克账号"
            return SimpleNamespace(
                session_id="quark-session",
                qr_code_data_url="data:image/svg+xml;base64,PHN2Zy8+",
                expires_in=300,
            )

        async def poll(self, session_id):
            assert session_id == "quark-session"
            return (
                "confirmed",
                SimpleNamespace(
                    account_id=None,
                    account_name="扫码夸克账号",
                    cookie="__pus=secret-pus; __kps=secret-kps",
                ),
            )

        async def finish(self, session_id):
            assert session_id == "quark-session"

    async def fake_verify_account(_db, account):
        account.account_identity = "Quark Drive"
        account.default_drive_id = "0"
        return account

    monkeypatch.setattr(cloud_accounts_api, "quark_qr_login", FakeQrLogin())
    monkeypatch.setattr(cloud_accounts_api, "verify_account", fake_verify_account)
    settings = get_settings()
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert login.status_code == 200
        started = client.post(
            "/api/v1/quark/qr-login/start",
            json={"name": "扫码夸克账号"},
        )
        assert started.status_code == 200
        completed = client.get("/api/v1/quark/qr-login/quark-session")

    assert completed.status_code == 200
    body = completed.json()
    assert body["status"] == "confirmed"
    assert body["account"]["provider"] == "quark"
    assert body["account"]["account_identity"] == "Quark Drive"
    assert "refresh_token" not in body["account"]
    assert "secret-pus" not in str(body)
