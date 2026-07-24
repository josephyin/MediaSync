from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.reconcile import run_reconciliation

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_v01_task_history_is_preserved_by_v2_migration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))

    try:
        command.upgrade(config, "0005_folder_checkpoints")
        engine = create_engine(database_url)
        now = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cloud_accounts (
                        id, provider, name, refresh_token, status, created_at, updated_at
                    ) VALUES (
                        1, 'aliyundrive', 'legacy-account', 'encrypted', 'active', :now, :now
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO subscriptions (
                        id, cloud_account_id, name, provider, share_url, target_path,
                        schedule, enabled, status, initial_sync_mode, created_at, updated_at
                    ) VALUES (
                        1, 1, 'legacy-subscription', 'aliyundrive',
                        'https://www.alipan.com/s/legacy', '/Media',
                        'interval:30m', 1, 'active', 'all', :now, :now
                    )
                    """
                ),
                {"now": now},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO tasks (
                        id, subscription_id, file_id, type, trigger_type, status,
                        idempotency_key, message, error_code, attempt_count, max_attempts,
                        started_at, finished_at, next_attempt_at, created_at, updated_at
                    ) VALUES (
                        1, 1, NULL, 'scan', 'scheduled', 'success',
                        'legacy-scan-1', 'legacy scan completed', NULL, 2, 3,
                        :now, :now, NULL, :now, :now
                    )
                    """
                ),
                {"now": now},
            )

        command.upgrade(config, "head")
        command.check(config)

        inspector = inspect(engine)
        task_columns = {column["name"] for column in inspector.get_columns("tasks")}
        assert {
            "account_id",
            "payload_version",
            "payload",
            "lock_token",
            "lease_until",
            "completed_at",
        }.issubset(task_columns)
        assert "task_runs" in inspector.get_table_names()

        with engine.connect() as connection:
            task = connection.execute(
                text(
                    """
                    SELECT account_id, payload_version, payload, status, message
                    FROM tasks
                    WHERE id = 1
                    """
                )
            ).mappings().one()
            task_run = connection.execute(
                text(
                    """
                    SELECT task_id, run_number, worker_id, status, result_summary
                    FROM task_runs
                    WHERE task_id = 1
                    """
                )
            ).mappings().one()

        assert task["account_id"] == 1
        assert task["payload_version"] == 1
        assert task["payload"] == "{}"
        assert task["status"] == "success"
        assert task["message"] == "legacy scan completed"
        assert task_run == {
            "task_id": 1,
            "run_number": 2,
            "worker_id": "legacy-v0.1",
            "status": "success",
            "result_summary": "legacy scan completed",
        }

        index_names = {index["name"] for index in inspector.get_indexes("tasks")}
        assert {
            "ix_tasks_queue",
            "ix_tasks_account_status",
            "ix_tasks_lease_recovery",
        }.issubset(index_names)
        run_uniques = inspector.get_unique_constraints("task_runs")
        assert any(
            constraint["name"] == "uq_task_runs_task_run_number"
            and constraint["column_names"] == ["task_id", "run_number"]
            for constraint in run_uniques
        )

        command.downgrade(config, "0005_folder_checkpoints")
        downgraded_inspector = inspect(engine)
        assert "task_runs" not in downgraded_inspector.get_table_names()
        assert "payload_version" not in {
            column["name"] for column in downgraded_inspector.get_columns("tasks")
        }
        with engine.connect() as connection:
            legacy_task = connection.execute(
                text("SELECT status, message FROM tasks WHERE id = 1")
            ).mappings().one()
        assert legacy_task == {
            "status": "success",
            "message": "legacy scan completed",
        }
    finally:
        get_settings.cache_clear()


def test_migrated_v01_running_task_is_reconciled_before_worker_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_path = tmp_path / "running-cutover.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    started_at = datetime(2026, 7, 24, 10, 0, tzinfo=UTC)
    reconciled_at = datetime(2026, 7, 24, 10, 1, tzinfo=UTC)

    try:
        command.upgrade(config, "0005_folder_checkpoints")
        engine = create_engine(database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO cloud_accounts (
                        id, provider, name, refresh_token, status, created_at, updated_at
                    ) VALUES (
                        1, 'aliyundrive', 'legacy-account', 'encrypted',
                        'active', :started_at, :started_at
                    )
                    """
                ),
                {"started_at": started_at},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO subscriptions (
                        id, cloud_account_id, name, provider, share_url, target_path,
                        schedule, enabled, status, initial_sync_mode, created_at, updated_at
                    ) VALUES (
                        1, 1, 'legacy-subscription', 'aliyundrive',
                        'https://www.alipan.com/s/legacy-running', '/Media',
                        'interval:30m', 1, 'active', 'all', :started_at, :started_at
                    )
                    """
                ),
                {"started_at": started_at},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO tasks (
                        id, subscription_id, file_id, type, trigger_type, status,
                        idempotency_key, message, error_code, attempt_count, max_attempts,
                        started_at, finished_at, next_attempt_at, created_at, updated_at
                    ) VALUES (
                        1, 1, NULL, 'scan', 'scheduled', 'running',
                        'legacy-running-scan', 'legacy scan in progress', NULL, 1, 3,
                        :started_at, NULL, NULL, :started_at, :started_at
                    )
                    """
                ),
                {"started_at": started_at},
            )

        command.upgrade(config, "head")
        with engine.connect() as connection:
            migrated_task = connection.execute(
                text(
                    """
                    SELECT status, retry_count, max_retries, locked_by, lock_token,
                           locked_at, lease_until
                    FROM tasks
                    WHERE id = 1
                    """
                )
            ).mappings().one()
            migrated_run = connection.execute(
                text(
                    """
                    SELECT status, run_number, worker_id, lock_token
                    FROM task_runs
                    WHERE task_id = 1
                    """
                )
            ).mappings().one()

        assert migrated_task == {
            "status": "running",
            "retry_count": 0,
            "max_retries": 2,
            "locked_by": None,
            "lock_token": None,
            "locked_at": None,
            "lease_until": None,
        }
        assert migrated_run == {
            "status": "running",
            "run_number": 1,
            "worker_id": "legacy-v0.1",
            "lock_token": None,
        }

        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        result = run_reconciliation(
            settings=Settings(
                _env_file=None,
                background_execution_mode="process",
                database_url=database_url,
                worker_retry_base_seconds=30,
                worker_retry_max_seconds=900,
            ),
            session_factory=sessions,
            clock=lambda: reconciled_at,
        )

        with engine.connect() as connection:
            reconciled_task = connection.execute(
                text(
                    """
                    SELECT status, retry_count, last_error_code, next_attempt_at
                    FROM tasks
                    WHERE id = 1
                    """
                )
            ).mappings().one()
            reconciled_run = connection.execute(
                text(
                    """
                    SELECT status, run_number, error_code, finished_at
                    FROM task_runs
                    WHERE task_id = 1
                    """
                )
            ).mappings().one()

        assert reconciled_task["status"] == "retry"
        assert reconciled_task["retry_count"] == 1
        assert reconciled_task["last_error_code"] == "LEGACY_CUTOVER_RECOVERY"
        assert datetime.fromisoformat(reconciled_task["next_attempt_at"]) == (
            reconciled_at + timedelta(seconds=30)
        ).replace(tzinfo=None)
        assert reconciled_run["status"] == "lost"
        assert reconciled_run["run_number"] == 1
        assert reconciled_run["error_code"] == "LEGACY_CUTOVER_RECOVERY"
        assert datetime.fromisoformat(
            reconciled_run["finished_at"]
        ) == reconciled_at.replace(tzinfo=None)
        assert result.orphan_retry_count == 1
        assert result.run_finalized_count == 1
    finally:
        get_settings.cache_clear()
