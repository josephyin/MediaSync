from __future__ import annotations

import asyncio
import logging
import os
import socket
from pathlib import Path

from app.core.config import get_settings
from app.services.docker_capability_service import DockerEngineClient
from app.services.update_snapshot_service import (
    UpdaterResultJournal,
    UpdateSnapshotService,
)
from app.services.updater_candidate_service import UpdaterCandidateService
from app.services.updater_coordinator_service import UpdaterCoordinator
from app.services.updater_forward_v2 import UpdaterForwardV2
from app.services.updater_rollback_v2 import UpdaterRollbackV2
from app.services.updater_state_machine import (
    ApplianceCommitWaiter,
    CandidateHealthVerifier,
    PreviousContainerHealthVerifier,
)

logger = logging.getLogger(__name__)


def build_coordinator() -> UpdaterCoordinator:
    settings = get_settings()
    operation_id = os.environ.get("MEDIASYNC_UPDATE_OPERATION_ID", "")
    pending_path = Path(settings.update_pending_path)
    data_directory = pending_path.parent.parent
    operations_directory = data_directory / "update" / "operations"
    engine = DockerEngineClient(
        socket_path=settings.docker_socket_path,
        timeout_seconds=settings.docker_api_timeout_seconds,
    )
    journal = UpdaterResultJournal(directory=str(operations_directory))
    snapshot = UpdateSnapshotService(data_directory=str(data_directory))
    candidate = UpdaterCandidateService(pending_path=pending_path)
    candidate_verifier = CandidateHealthVerifier(
        engine=engine,
        data_directory=data_directory,
        pending_path=pending_path,
    )
    previous_verifier = PreviousContainerHealthVerifier(engine=engine)
    commit_waiter = ApplianceCommitWaiter(
        data_directory=data_directory,
        pending_path=pending_path,
        timeout_seconds=300,
        poll_seconds=2,
    )

    def forward_factory(coordinator_id: str) -> UpdaterForwardV2:
        return UpdaterForwardV2(
            engine=engine,
            data_directory=data_directory,
            socket_path=settings.docker_socket_path,
            coordinator_container_id=coordinator_id,
            snapshot_service=snapshot,
            candidate_service=candidate,
            journal=journal,
            verifier=candidate_verifier,
            commit_waiter=commit_waiter,
        )

    def rollback_factory(coordinator_id: str) -> UpdaterRollbackV2:
        return UpdaterRollbackV2(
            engine=engine,
            data_directory=data_directory,
            socket_path=settings.docker_socket_path,
            coordinator_container_id=coordinator_id,
            snapshot_service=snapshot,
            candidate_service=candidate,
            journal=journal,
            previous_verifier=previous_verifier,
        )

    return UpdaterCoordinator(
        engine=engine,
        data_directory=data_directory,
        pending_path=pending_path,
        socket_path=settings.docker_socket_path,
        hostname=socket.gethostname(),
        operation_id=operation_id,
        journal=journal,
        candidate_service=candidate,
        forward_factory=forward_factory,
        rollback_factory=rollback_factory,
    )


async def run() -> int:
    coordinator = build_coordinator()
    while True:
        outcome = await coordinator.run_once()
        logger.info(
            "updater_coordinator_outcome outcome=%s",
            outcome,
        )
        if outcome == "completed":
            return 0
        if outcome == "manual_recovery":
            return 2
        await asyncio.sleep(5)


def main() -> int:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(run())
    except Exception:
        logger.exception("updater_coordinator_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
