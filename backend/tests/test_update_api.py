from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.main import app
from app.repositories import UpdateOperationRepository
from app.schemas.update import (
    DockerCapabilityInfo,
    ManualUpgradeInfo,
    ReleaseInfo,
    UpdateStatusRead,
)


class FakeUpdateCheckService:
    def __init__(self) -> None:
        self.check_calls = 0
        self._status = self._unchecked()

    def get_status(self) -> UpdateStatusRead:
        return self._status

    @staticmethod
    def _unchecked() -> UpdateStatusRead:
        return UpdateStatusRead(
            current_version="0.2.0-rc.9",
            channel="rc",
            status="not_checked",
            install_unavailable_reason="当前版本仅提供检查更新",
            docker_capability=DockerCapabilityInfo(
                reason_code="not_probed",
                message="尚未探测 Docker 更新能力",
            ),
            manual_upgrade=ManualUpgradeInfo(
                image="josephyjq/mediasync:rc",
                message="请在 NAS 容器管理器中更新",
            ),
        )

    async def check(self) -> UpdateStatusRead:
        self.check_calls += 1
        checked_at = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
        self._status = UpdateStatusRead(
            current_version="0.2.0-rc.9",
            channel="rc",
            status="update_available",
            install_unavailable_reason="当前版本仅提供检查更新",
            docker_capability=DockerCapabilityInfo(
                reason_code="not_probed",
                message="尚未探测 Docker 更新能力",
            ),
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
        return self._status


class FakeDockerCapabilityService:
    async def probe(self) -> DockerCapabilityInfo:
        return DockerCapabilityInfo(
            socket_available=True,
            engine_available=True,
            container_identified=True,
            reason_code="ready",
            message="Docker 环境与当前 MediaSync 容器已安全识别",
        )


class FakeUpdateInstallService:
    def __init__(self) -> None:
        self.executed: list[tuple[str, str]] = []

    def begin(self, session, *, source_version: str, target_version: str):
        operation = UpdateOperationRepository(session).create(
            source_version=source_version,
            status="pulling",
            target_version=target_version,
        )
        session.commit()
        session.refresh(operation)
        return operation

    async def execute(self, *, operation_id: str, target_version: str) -> None:
        self.executed.append((operation_id, target_version))
        with SessionLocal() as session, session.begin():
            repository = UpdateOperationRepository(session)
            operation = repository.get_by_operation_id(operation_id)
            assert operation is not None
            repository.finish(
                operation,
                status="failed",
                error_code="test_finished",
                error_message="测试后台任务已结束",
            )


def test_update_endpoints_require_admin_and_return_service_status(monkeypatch) -> None:
    from app.api.v1 import system

    fake = FakeUpdateCheckService()
    monkeypatch.setattr(system, "get_update_check_service", lambda: fake)
    monkeypatch.setattr(
        system,
        "get_docker_capability_service",
        lambda: FakeDockerCapabilityService(),
    )
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
        assert checked.json()["install_supported"] is True
        assert checked.json()["docker_socket_enabled"] is True
        assert checked.json()["docker_capability"]["reason_code"] == "ready"
        assert fake.check_calls == 1


def test_install_update_requires_exact_checked_target_and_runs_in_background(
    monkeypatch,
) -> None:
    from app.api.v1 import system

    fake_check = FakeUpdateCheckService()
    fake_install = FakeUpdateInstallService()
    monkeypatch.setattr(system, "get_update_check_service", lambda: fake_check)
    monkeypatch.setattr(
        system,
        "get_docker_capability_service",
        lambda: FakeDockerCapabilityService(),
    )
    monkeypatch.setattr(system, "get_update_install_service", lambda: fake_install)
    settings = get_settings()

    with TestClient(app) as client:
        assert client.post(
            "/api/v1/system/update/install",
            json={"target_version": "v0.3.0-rc.1"},
        ).status_code == 401
        client.post(
            "/api/v1/auth/login",
            json={"username": settings.admin_username, "password": settings.admin_password},
        )
        assert client.post("/api/v1/system/update/check").status_code == 200

        stale = client.post(
            "/api/v1/system/update/install",
            json={"target_version": "v0.3.0-rc.2"},
        )
        assert stale.status_code == 409

        accepted = client.post(
            "/api/v1/system/update/install",
            json={"target_version": "v0.3.0-rc.1"},
        )
        assert accepted.status_code == 202
        assert accepted.json()["operation"]["status"] == "pulling"
        assert len(fake_install.executed) == 1
        assert fake_install.executed[0][1] == "v0.3.0-rc.1"
