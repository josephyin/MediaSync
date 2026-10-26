import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.core.exceptions import ProviderRequestError
from app.core.security import require_admin
from app.main import app
from app.models import Base, CloudAccount
from app.providers.pikpak.login import login_with_password
from app.services.account_service import get_decrypted_token


async def test_password_login_returns_only_refresh_credential():
    calls = []

    def handler(request):
        calls.append(request)
        body = json.loads(request.content)
        assert request.url.host == "user.mypikpak.com"
        if request.url.path.endswith("/captcha/init"):
            assert body["meta"] == {"email": "test@example.com"}
            assert "password" not in body
            return httpx.Response(200, json={"captcha_token": "captcha"})
        assert body["password"] == "private-password"
        assert body["captcha_token"] == "captcha"
        assert request.headers["X-Device-Id"] == json.loads(calls[0].content)["device_id"]
        return httpx.Response(
            200, json={"refresh_token": "refresh", "sub": "user", "access_token": "access"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential, user = await login_with_password(
            "test@example.com", "private-password", http_client=client
        )
    assert user == "user"
    assert json.loads(credential)["refresh_token"] == "refresh"
    assert "private-password" not in credential
    assert "access_token" not in credential
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"url": "https://example.com/challenge", "captcha_token": "pending"},
        {},
    ],
)
async def test_captcha_challenge_does_not_submit_password(response):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderRequestError, match="人机验证"):
            await login_with_password("test@example.com", "secret", http_client=client)
    assert len(requests) == 1
    assert "secret" not in requests[0].content.decode()


async def test_signin_failure_sanitized_and_not_retried():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"captcha_token": "captcha"})
        return httpx.Response(
            400,
            json={"error": "invalid_account_or_password", "error_description": "private-password"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderRequestError) as exc:
            await login_with_password("test@example.com", "private-password", http_client=client)
    assert "private-password" not in str(exc.value)
    assert count == 2


def test_login_api_auth_encryption_and_duplicate(monkeypatch):
    from app.api.v1 import cloud_accounts

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    calls = []

    async def login(username, password):
        calls.append((username, password))
        return '{"refresh_token":"test-refresh"}', (
            "other" if username == "other@example.com" else "user"
        )

    def db_session():
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(cloud_accounts, "login_with_password", login)
    app.dependency_overrides[get_db] = db_session
    try:
        with TestClient(app) as client:
            payload = {
                "name": "test",
                "username": "test@example.com",
                "password": "private-password",
            }
            assert client.post("/api/v1/pikpak/login", json=payload).status_code == 401
            assert calls == []
            app.dependency_overrides[require_admin] = lambda: "admin"
            result = client.post("/api/v1/pikpak/login", json=payload)
            assert result.status_code == 201, result.text
            assert result.json()["provider"] == "pikpak"
            assert result.json()["last_verified_at"] is None
            assert "private-password" not in result.text
            assert "test-refresh" not in result.text
            assert client.post("/api/v1/pikpak/login", json=payload).status_code == 409
            assert len(calls) == 1
            account_id = result.json()["id"]
            renewed = client.post(
                "/api/v1/pikpak/login", json={**payload, "account_id": account_id}
            )
            assert renewed.status_code == 201
            assert renewed.json()["id"] == account_id
            rejected = client.post(
                "/api/v1/pikpak/login",
                json={**payload, "account_id": account_id, "username": "other@example.com"},
            )
            assert rejected.status_code == 409
            with Session(engine) as db:
                account = db.scalar(select(CloudAccount))
                assert "test-refresh" not in account.refresh_token
                assert json.loads(get_decrypted_token(account))["refresh_token"] == "test-refresh"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(require_admin, None)
        engine.dispose()
