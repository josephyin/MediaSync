from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app
from app.schemas.update import ManualUpgradeInfo, ReleaseInfo, UpdateStatusRead


class FakeUpdateCheckService:
    def __init__(self) -> None:
        self.check_calls = 0

    def get_status(self) -> UpdateStatusRead:
        return UpdateStatusRead(
            current_version="0.2.0-rc.9",
            channel="rc",
            status="not_checked",
            install_unavailable_reason="当前版本仅提供检查更新",
            manual_upgrade=ManualUpgradeInfo(
                image="josephyjq/mediasync:rc",
                message="请在 NAS 容器管理器中更新",
            ),
        )

    async def check(self) -> UpdateStatusRead:
        self.check_calls += 1
        checked_at = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
        return UpdateStatusRead(
            current_version="0.2.0-rc.9",
            channel="rc",
            status="update_available",
            install_unavailable_reason="当前版本仅提供检查更新",
            latest_release=ReleaseInfo(
                version="0.3.0-rc.1",
                tag_name="v0.3.0-rc.1",
                published_at=checked_at,
                release_url=(
                    "https://github.com/josephyin/MediaSync/releases/tag/v0.3.0-rc.1"
                ),
                notes="测试更新",
                prerelease=True,
            ),
            checked_at=checked_at,
            last_success_at=checked_at,
            manual_upgrade=ManualUpgradeInfo(
                image="josephyjq/mediasync:v0.3.0-rc.1",
                message="请在 NAS 容器管理器中更新",
            ),
        )


def test_update_endpoints_require_admin_and_return_service_status(monkeypatch) -> None:
    from app.api.v1 import system

    fake = FakeUpdateCheckService()
    monkeypatch.setattr(system, "get_update_check_service", lambda: fake)
    settings = get_settings()

    with TestClient(app) as client:
        assert client.get("/api/v1/system/update").status_code == 401
        assert client.post("/api/v1/system/update/check").status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert login.status_code == 200

        initial = client.get("/api/v1/system/update")
        assert initial.status_code == 200
        assert initial.json()["status"] == "not_checked"

        checked = client.post("/api/v1/system/update/check")
        assert checked.status_code == 200
        assert checked.json()["status"] == "update_available"
        assert checked.json()["latest_release"]["version"] == "0.3.0-rc.1"
        assert checked.json()["install_supported"] is False
        assert fake.check_calls == 1

