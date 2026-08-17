from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base
from app.repositories import UpdateOperationRepository
from app.services.image_target_service import VerifiedImageTarget
from app.services.update_install_service import UpdateInstallService

TARGET = VerifiedImageTarget(
    registry="dockerhub",
    repository="josephyjq/mediasync",
    version="v0.2.0-rc.11",
    digest=f"sha256:{'a' * 64}",
    revision="b" * 40,
)


class FakeImageService:
    async def pull_and_verify(self, *, registry_key: str, version: str):
        assert registry_key == "dockerhub"
        assert version == TARGET.version
        return TARGET


class FakeEngine:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.removed: list[str] = []

    async def inspect_container(self, container_id: str):
        assert container_id == "c" * 64
        return {"Id": container_id}

    async def start_container(self, container_id: str) -> None:
        self.started.append(container_id)

    async def remove_container(self, container_id: str) -> None:
        self.removed.append(container_id)


class FakeContainerResolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_current_container(self):
        self.calls += 1
        return {"Id": "c" * 64}


class FakeHandoffService:
    def __init__(self, tmp_path: Path, *, fail: bool = False) -> None:
        self.path = tmp_path / "operation.handoff.json"
        self.fail = fail

    async def prepare(self, *, operation_id: str, current_container, target):
        assert current_container == {"Id": "c" * 64}
        assert target == TARGET
        if self.fail:
            raise RuntimeError("无法准备 helper")
        self.path.write_text(operation_id, encoding="utf-8")
        return "d" * 64, self.path


def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'mediasync.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def build_service(
    tmp_path: Path,
    factory: sessionmaker[Session],
    engine: FakeEngine,
    handoff: FakeHandoffService,
    resolver: FakeContainerResolver | None = None,
) -> UpdateInstallService:
    return UpdateInstallService(
        session_factory=factory,
        image_service=FakeImageService(),  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        handoff_service=handoff,  # type: ignore[arg-type]
        container_resolver=resolver or FakeContainerResolver(),
        registry_key="dockerhub",
        drain_timeout_seconds=1,
        drain_poll_seconds=0.01,
    )


async def test_install_prepares_handoff_after_drain_and_starts_helper(tmp_path: Path) -> None:
    factory = session_factory(tmp_path)
    engine = FakeEngine()
    handoff = FakeHandoffService(tmp_path)
    resolver = FakeContainerResolver()
    service = build_service(tmp_path, factory, engine, handoff, resolver)
    with factory() as session:
        operation = service.begin(
            session,
            source_version="0.2.0-rc.10",
            target_version=TARGET.version,
        )
        operation_id = operation.operation_id

    await service.execute(operation_id=operation_id, target_version=TARGET.version)

    with factory() as session:
        stored = UpdateOperationRepository(session).get_by_operation_id(operation_id)
        assert stored is not None
        assert stored.status == "handoff"
        assert stored.target_digest == TARGET.digest
    assert engine.started == ["d" * 64]
    assert handoff.path.exists()
    assert resolver.calls == 1


async def test_install_failure_before_helper_start_is_terminal_and_retryable(
    tmp_path: Path,
) -> None:
    factory = session_factory(tmp_path)
    engine = FakeEngine()
    handoff = FakeHandoffService(tmp_path, fail=True)
    service = build_service(tmp_path, factory, engine, handoff)
    with factory() as session:
        operation = service.begin(
            session,
            source_version="0.2.0-rc.10",
            target_version=TARGET.version,
        )
        operation_id = operation.operation_id

    await service.execute(operation_id=operation_id, target_version=TARGET.version)

    with factory() as session:
        stored = UpdateOperationRepository(session).get_by_operation_id(operation_id)
        assert stored is not None
        assert stored.status == "failed"
        assert stored.active_slot is None
        assert stored.error_code == "update_install_failed"
        assert "无法准备 helper" in (stored.error_message or "")
    assert engine.started == []
