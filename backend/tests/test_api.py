from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.admin_credentials import AdminCredentialStore
from app.core.config import get_settings
from app.core.runtime_secrets import (
    RUNTIME_CONFIG_DIRECTORY,
    RUNTIME_SECRETS_FILENAME,
    prepare_runtime_secrets,
)
from app.main import app


def test_health_is_public() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_default_api_mode_reports_online_password_change_as_unsupported() -> None:
    settings = get_settings()
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert login.status_code == 200
        assert login.json()["password_change_supported"] is False

        response = client.post(
            "/api/v1/auth/password",
            json={
                "current_password": settings.admin_password,
                "new_password": "new-password-for-compose",
                "confirm_password": "new-password-for-compose",
            },
        )
        assert response.status_code == 409


def test_appliance_password_change_invalidates_all_old_sessions(
    tmp_path,
    monkeypatch,
) -> None:
    from app.api.v1 import auth as auth_api
    from app.core import security

    old_password = "old-appliance-password"
    new_password = "new-appliance-password"
    prepared = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": "test-secret-key-for-password-change",
            "CREDENTIAL_ENCRYPTION_KEY": "test-credential-key-for-password-change",
            "ADMIN_PASSWORD": old_password,
        },
    )
    secrets_path = (
        tmp_path / RUNTIME_CONFIG_DIRECTORY / RUNTIME_SECRETS_FILENAME
    )
    store = AdminCredentialStore(
        password=prepared.values.admin_password,
        revision=prepared.values.admin_session_revision,
        runtime_secrets_path=secrets_path,
    )
    monkeypatch.setattr(auth_api, "get_admin_credential_store", lambda: store)
    monkeypatch.setattr(security, "get_admin_credential_store", lambda: store)

    with TestClient(app) as first_client, TestClient(app) as second_client:
        for client in (first_client, second_client):
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": old_password},
            )
            assert login.status_code == 200
            assert login.json()["password_change_supported"] is True

        changed = first_client.post(
            "/api/v1/auth/password",
            json={
                "current_password": old_password,
                "new_password": new_password,
                "confirm_password": new_password,
            },
        )
        assert changed.status_code == 200
        assert changed.json() == {"message": "密码已修改，请重新登录"}
        assert first_client.get("/api/v1/auth/status").json()["authenticated"] is False
        assert second_client.get("/api/v1/auth/status").json()["authenticated"] is False

        old_login = first_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": old_password},
        )
        new_login = first_client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": new_password},
        )
        assert old_login.status_code == 401
        assert new_login.status_code == 200


def test_admin_login_and_account_round_trip() -> None:
    settings = get_settings()
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/cloud-accounts")
        assert unauthorized.status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert login.status_code == 200

        created = client.post(
            "/api/v1/cloud-accounts",
            json={
                "provider": "aliyundrive",
                "name": "test-account",
                "refresh_token": "test-refresh-token",
            },
        )
        assert created.status_code in {201, 409}

        accounts = client.get("/api/v1/cloud-accounts")
        assert accounts.status_code == 200
        body = accounts.json()
        assert body["total"] >= 1
        assert "refresh_token" not in body["items"][0]


def test_account_can_be_edited_and_deleted() -> None:
    settings = get_settings()
    unique = uuid4().hex
    with TestClient(app) as client:
        client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        created = client.post(
            "/api/v1/cloud-accounts",
            json={
                "provider": "aliyundrive",
                "name": f"edit-{unique}",
                "refresh_token": "old-token",
            },
        )
        assert created.status_code == 201
        account_id = created.json()["id"]

        updated = client.patch(
            f"/api/v1/cloud-accounts/{account_id}",
            json={"name": f"updated-{unique}", "refresh_token": "new-token"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == f"updated-{unique}"

        deleted = client.delete(f"/api/v1/cloud-accounts/{account_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/cloud-accounts/{account_id}").status_code == 404
