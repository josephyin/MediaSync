from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import reconcile as reconcile_module
from app.core.config import Settings
from app.core.execution import BackgroundExecutionModeError
from app.models import Base, Task
from app.reconcile import run_reconciliation

NOW = datetime(2026, 7, 24, 17, 0, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    database_path = tmp_path / "reconciliation-process.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def process_settings() -> Settings:
    return Settings(
        _env_file=None,
        background_execution_mode="process",
        worker_retry_base_seconds=10,
        worker_retry_max_seconds=120,
    )


def test_reconciliation_refuses_legacy_mode_before_session_construction() -> None:
    def unexpected_session() -> Session:
        raise AssertionError("database must not be opened in legacy mode")

    with pytest.raises(BackgroundExecutionModeError):
        run_reconciliation(
            settings=Settings(
                _env_file=None,
                background_execution_mode="legacy",
            ),
            session_factory=unexpected_session,
            clock=lambda: NOW,
        )


def test_reconciliation_command_commits_successful_result(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session, session.begin():
        task = Task(
            type="scan",
            status="running",
            attempt_count=1,
            max_retries=2,
            started_at=NOW,
        )
        session.add(task)
        session.flush()
        task_id = task.id

    result = run_reconciliation(
        settings=process_settings(),
        session_factory=sessions,
        clock=lambda: NOW,
    )

    with sessions() as session:
        task = session.get(Task, task_id)
        assert task is not None
        assert task.status == "retry"
        assert result.orphan_retry_count == 1


def test_main_exits_two_for_mode_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reconcile_module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            background_execution_mode="legacy",
        ),
    )

    with pytest.raises(SystemExit) as raised:
        reconcile_module.main()

    assert raised.value.code == 2


def test_main_exits_one_for_reconciliation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconcile_module, "get_settings", process_settings)
    monkeypatch.setattr(
        reconcile_module,
        "run_reconciliation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(SystemExit) as raised:
        reconcile_module.main()

    assert raised.value.code == 1
