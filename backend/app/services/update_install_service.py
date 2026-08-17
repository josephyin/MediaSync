from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Task, UpdateOperation
from app.repositories import UpdateOperationRepository
from app.services.docker_capability_service import (
    DockerEngineClient,
    get_docker_capability_service,
)
from app.services.image_target_service import ImageTargetService, get_image_target_service
from app.services.updater_handoff_service import (
    UpdaterHandoffService,
    UpdaterHandoffStore,
)

logger = logging.getLogger(__name__)


class UpdateInstallError(RuntimeError):
    pass


class CurrentContainerResolver(Protocol):
    async def resolve_current_container(self) -> dict[str, Any]: ...


class UpdateInstallService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        image_service: ImageTargetService,
        engine: DockerEngineClient,
        handoff_service: UpdaterHandoffService,
        container_resolver: CurrentContainerResolver,
        registry_key: str,
        drain_timeout_seconds: float,
        drain_poll_seconds: float,
    ) -> None:
        self._session_factory = session_factory
        self._image_service = image_service
        self._engine = engine
        self._handoff_service = handoff_service
        self._container_resolver = container_resolver
        self._registry_key = registry_key
        self._drain_timeout_seconds = drain_timeout_seconds
        self._drain_poll_seconds = drain_poll_seconds

    def begin(
        self,
        session: Session,
        *,
        source_version: str,
        target_version: str,
    ) -> UpdateOperation:
        operation = UpdateOperationRepository(session).create(
            source_version=source_version,
            status="pulling",
            target_version=target_version,
        )
        session.commit()
        session.refresh(operation)
        return operation

    async def execute(self, *, operation_id: str, target_version: str) -> None:
        helper_id: str | None = None
        handoff_path: Path | None = None
        try:
            target = await self._image_service.pull_and_verify(
                registry_key=self._registry_key,
                version=target_version,
            )
            with self._session_factory() as session, session.begin():
                repository = UpdateOperationRepository(session)
                operation = self._require_active(repository, operation_id, "pulling")
                repository.set_verified_target(
                    operation,
                    target_version=target.version,
                    target_digest=target.digest,
                )
                repository.transition_active(operation, status="draining")

            await self._wait_until_drained()
            current = await self._container_resolver.resolve_current_container()
            helper_id, handoff_path = await self._handoff_service.prepare(
                operation_id=operation_id,
                current_container=current,
                target=target,
            )
            with self._session_factory() as session, session.begin():
                repository = UpdateOperationRepository(session)
                operation = self._require_active(repository, operation_id, "draining")
                repository.transition_active(operation, status="handoff")
            await self._engine.start_container(helper_id)
            logger.info(
                "update_install_handoff_started operation_id=%s target_version=%s",
                operation_id,
                target.version,
            )
        except Exception as exc:
            logger.exception(
                "update_install_failed operation_id=%s phase=prepare_or_handoff",
                operation_id,
            )
            if helper_id is not None:
                try:
                    await self._engine.remove_container(helper_id)
                except Exception:
                    logger.warning(
                        "update_install_helper_cleanup_failed operation_id=%s",
                        operation_id,
                    )
            if handoff_path is not None:
                handoff_path.unlink(missing_ok=True)
            self._finish_failed(operation_id, exc)

    async def _wait_until_drained(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._drain_timeout_seconds
        while True:
            with self._session_factory() as session:
                active = session.scalar(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        or_(
                            Task.lock_token.is_not(None),
                            Task.status.in_(("running", "cancel_requested")),
                        )
                    )
                )
            if not active:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise UpdateInstallError("等待当前任务结束超时，请稍后重试")
            await asyncio.sleep(self._drain_poll_seconds)

    @staticmethod
    def _require_active(
        repository: UpdateOperationRepository,
        operation_id: str,
        expected_status: str,
    ) -> UpdateOperation:
        operation = repository.get_by_operation_id(operation_id)
        if (
            operation is None
            or operation.active_slot != "global"
            or operation.status != expected_status
        ):
            raise UpdateInstallError("更新操作状态已经变化")
        return operation

    def _finish_failed(self, operation_id: str, exc: Exception) -> None:
        try:
            with self._session_factory() as session, session.begin():
                repository = UpdateOperationRepository(session)
                operation = repository.get_by_operation_id(operation_id)
                if operation is None or operation.active_slot != "global":
                    return
                repository.finish(
                    operation,
                    status="failed",
                    error_code="update_install_failed",
                    error_message=str(exc)[:1000] or "启动更新失败",
                )
        except Exception:
            logger.exception(
                "update_install_failure_persist_failed operation_id=%s",
                operation_id,
            )


@lru_cache
def get_update_install_service() -> UpdateInstallService:
    from app.core.config import get_settings
    from app.core.database import SessionLocal

    settings = get_settings()
    engine = DockerEngineClient(
        socket_path=settings.docker_socket_path,
        timeout_seconds=settings.docker_api_timeout_seconds,
    )
    operations_directory = Path(settings.update_pending_path).parent / "operations"
    return UpdateInstallService(
        session_factory=SessionLocal,
        image_service=get_image_target_service(),
        engine=engine,
        handoff_service=UpdaterHandoffService(
            engine=engine,
            store=UpdaterHandoffStore(directory=str(operations_directory)),
            socket_path=settings.docker_socket_path,
        ),
        container_resolver=get_docker_capability_service(),
        registry_key=settings.update_image_registry,
        drain_timeout_seconds=settings.update_drain_timeout_seconds,
        drain_poll_seconds=settings.update_drain_poll_seconds,
    )
