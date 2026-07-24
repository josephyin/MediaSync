from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app


def test_health_is_public() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
