from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.execution import (
    BackgroundExecutionModeError,
    require_background_execution_mode,
)
from app.models.base import utcnow
from app.services.legacy_task_reconciliation import (
    LegacyTaskReconciliationResult,
    reconcile_legacy_tasks,
)
from app.task_engine.worker import ExponentialBackoff

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


def run_reconciliation(
    *,
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> LegacyTaskReconciliationResult:
    resolved_settings = settings or get_settings()
    logger.info(
        "background_execution_mode_selected process=cutover mode=%s",
        resolved_settings.background_execution_mode,
    )
    require_background_execution_mode(
        resolved_settings,
        process_name="cutover",
        expected_mode="process",
    )
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal

    reconciled_at = clock()
    retry_backoff = ExponentialBackoff(
        base_delay=timedelta(seconds=resolved_settings.worker_retry_base_seconds),
        max_delay=timedelta(seconds=resolved_settings.worker_retry_max_seconds),
    )
    with session_factory() as session, session.begin():
        result = reconcile_legacy_tasks(
            session,
            reconciled_at=reconciled_at,
            retry_backoff=retry_backoff,
        )
    logger.info(
        "legacy_reconciliation_completed inspected_count=%d "
        "orphan_retry_count=%d orphan_failed_count=%d "
        "run_appended_count=%d run_finalized_count=%d run_preserved_count=%d "
        "preserved_by_status=%s",
        result.inspected_count,
        result.orphan_retry_count,
        result.orphan_failed_count,
        result.run_appended_count,
        result.run_finalized_count,
        result.run_preserved_count,
        result.preserved_by_status,
    )
    return result


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        run_reconciliation(settings=settings)
    except BackgroundExecutionModeError as exc:
        logger.error(
            "process_start_refused process=%s expected_mode=%s actual_mode=%s",
            exc.process_name,
            exc.expected_mode,
            exc.actual_mode,
        )
        raise SystemExit(2) from None
    except Exception:
        logger.exception("legacy_reconciliation_failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
