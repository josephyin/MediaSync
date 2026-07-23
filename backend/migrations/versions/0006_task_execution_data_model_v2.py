"""Introduce Task Execution Data Model v2."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_task_execution_data_model_v2"
down_revision: str | None = "0005_folder_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TASK_STATUS_SQL = (
    "'pending', 'running', 'retry', 'waiting_credential', "
    "'cancel_requested', 'success', 'failed', 'cancelled'"
)
TASK_RUN_STATUS_SQL = "'running', 'success', 'failed', 'blocked', 'lost', 'cancelled'"


def upgrade() -> None:
    with op.batch_alter_table(
        "tasks",
        naming_convention=NAMING_CONVENTION,
        recreate="always",
    ) as batch_op:
        batch_op.add_column(sa.Column("account_id", sa.Integer()))
        batch_op.add_column(
            sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column(
                "payload_version",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "payload",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "retry_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "max_retries",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("3"),
            )
        )
        batch_op.add_column(sa.Column("blocked_reason", sa.String(100)))
        batch_op.add_column(sa.Column("blocked_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("locked_by", sa.String(128)))
        batch_op.add_column(sa.Column("lock_token", sa.String(64)))
        batch_op.add_column(sa.Column("locked_at", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("lease_until", sa.DateTime(timezone=True)))
        batch_op.add_column(sa.Column("last_error_code", sa.String(100)))
        batch_op.add_column(sa.Column("last_error_message", sa.Text()))
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True)))
        batch_op.create_foreign_key(
            "fk_tasks_account_id_cloud_accounts",
            "cloud_accounts",
            ["account_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "status_v2",
            f"status IN ({TASK_STATUS_SQL})",
        )
        batch_op.create_check_constraint("priority_nonnegative", "priority >= 0")
        batch_op.create_check_constraint(
            "payload_version_positive",
            "payload_version >= 1",
        )
        batch_op.create_check_constraint("retry_count_nonnegative", "retry_count >= 0")
        batch_op.create_check_constraint("max_retries_nonnegative", "max_retries >= 0")
        batch_op.create_check_constraint(
            "ownership_fields_complete",
            """
            (
                locked_by IS NULL
                AND lock_token IS NULL
                AND locked_at IS NULL
                AND lease_until IS NULL
            )
            OR
            (
                locked_by IS NOT NULL
                AND lock_token IS NOT NULL
                AND locked_at IS NOT NULL
                AND lease_until IS NOT NULL
            )
            """,
        )
        batch_op.create_index(
            "ix_tasks_queue",
            ["status", "next_attempt_at", "priority", "created_at"],
        )
        batch_op.create_index("ix_tasks_account_status", ["account_id", "status"])
        batch_op.create_index("ix_tasks_lease_recovery", ["status", "lease_until"])

    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET
                account_id = (
                    SELECT subscriptions.cloud_account_id
                    FROM subscriptions
                    WHERE subscriptions.id = tasks.subscription_id
                ),
                retry_count = CASE
                    WHEN status = 'failed' AND attempt_count > 0 THEN attempt_count
                    WHEN status = 'failed' THEN 1
                    WHEN status = 'pending' AND attempt_count > 0 THEN attempt_count
                    ELSE 0
                END,
                max_retries = CASE
                    WHEN max_attempts > 0 THEN max_attempts - 1
                    ELSE 0
                END,
                last_error_code = error_code,
                last_error_message = CASE
                    WHEN error_code IS NOT NULL OR status = 'failed' THEN message
                    ELSE NULL
                END,
                completed_at = CASE
                    WHEN status IN ('success', 'failed', 'cancelled') THEN finished_at
                    ELSE NULL
                END
            """
        )
    )

    op.create_table(
        "task_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lock_token", sa.String(64)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result_summary", sa.Text()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "metrics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "task_id",
            "run_number",
            name="uq_task_runs_task_run_number",
        ),
        sa.CheckConstraint(
            f"status IN ({TASK_RUN_STATUS_SQL})",
            name="ck_task_runs_status",
        ),
        sa.CheckConstraint(
            "run_number >= 1",
            name="ck_task_runs_run_number_positive",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_task_runs_duration_nonnegative",
        ),
    )
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])
    op.create_index(
        "ix_task_runs_task_created",
        "task_runs",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_task_runs_status_started",
        "task_runs",
        ["status", "started_at"],
    )

    op.execute(
        sa.text(
            """
            INSERT INTO task_runs (
                task_id,
                run_number,
                worker_id,
                lock_token,
                status,
                started_at,
                finished_at,
                last_heartbeat_at,
                duration_ms,
                result_summary,
                error_code,
                error_message,
                metrics,
                created_at,
                updated_at
            )
            SELECT
                id,
                CASE WHEN attempt_count > 0 THEN attempt_count ELSE 1 END,
                'legacy-v0.1',
                NULL,
                CASE
                    WHEN status = 'running' THEN 'running'
                    WHEN status = 'success' THEN 'success'
                    WHEN status = 'cancelled' THEN 'cancelled'
                    ELSE 'failed'
                END,
                COALESCE(started_at, created_at),
                CASE
                    WHEN status = 'running' THEN NULL
                    ELSE COALESCE(finished_at, updated_at)
                END,
                NULL,
                NULL,
                CASE WHEN status = 'success' THEN message ELSE NULL END,
                error_code,
                CASE
                    WHEN status = 'failed'
                        OR (status = 'pending' AND attempt_count > 0)
                    THEN message
                    ELSE NULL
                END,
                '{"schema_version": 1, "migrated_from": "v0.1"}',
                created_at,
                updated_at
            FROM tasks
            WHERE
                status IN ('running', 'success', 'failed', 'cancelled')
                OR attempt_count > 0
                OR started_at IS NOT NULL
                OR finished_at IS NOT NULL
            """
        )
    )

    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_tasks_payload_immutable
            BEFORE UPDATE OF payload, payload_version ON tasks
            FOR EACH ROW
            WHEN NEW.payload IS NOT OLD.payload
                OR NEW.payload_version IS NOT OLD.payload_version
            BEGIN
                SELECT RAISE(ABORT, 'task payload and version are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_tasks_idempotency_key_immutable
            BEFORE UPDATE OF idempotency_key ON tasks
            FOR EACH ROW
            WHEN OLD.idempotency_key IS NOT NULL
                AND NEW.idempotency_key IS NOT OLD.idempotency_key
            BEGIN
                SELECT RAISE(ABORT, 'task idempotency key is immutable once assigned');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_task_runs_identity_immutable
            BEFORE UPDATE OF task_id, run_number ON task_runs
            FOR EACH ROW
            WHEN NEW.task_id IS NOT OLD.task_id
                OR NEW.run_number IS NOT OLD.run_number
            BEGIN
                SELECT RAISE(ABORT, 'task run identity is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_task_runs_terminal_immutable
            BEFORE UPDATE ON task_runs
            FOR EACH ROW
            WHEN OLD.status IN ('success', 'failed', 'blocked', 'lost', 'cancelled')
            BEGIN
                SELECT RAISE(ABORT, 'terminal task run is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_task_runs_no_delete
            BEFORE DELETE ON task_runs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'task run history cannot be deleted');
            END
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_task_runs_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_task_runs_terminal_immutable")
        op.execute("DROP TRIGGER IF EXISTS trg_task_runs_identity_immutable")
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_idempotency_key_immutable")
        op.execute("DROP TRIGGER IF EXISTS trg_tasks_payload_immutable")

    op.drop_index("ix_task_runs_status_started", table_name="task_runs")
    op.drop_index("ix_task_runs_task_created", table_name="task_runs")
    op.drop_index("ix_task_runs_task_id", table_name="task_runs")
    op.drop_table("task_runs")

    with op.batch_alter_table(
        "tasks",
        naming_convention=NAMING_CONVENTION,
        recreate="always",
    ) as batch_op:
        batch_op.drop_index("ix_tasks_lease_recovery")
        batch_op.drop_index("ix_tasks_account_status")
        batch_op.drop_index("ix_tasks_queue")
        batch_op.drop_constraint("ck_tasks_ownership_fields_complete", type_="check")
        batch_op.drop_constraint("ck_tasks_max_retries_nonnegative", type_="check")
        batch_op.drop_constraint("ck_tasks_retry_count_nonnegative", type_="check")
        batch_op.drop_constraint("ck_tasks_payload_version_positive", type_="check")
        batch_op.drop_constraint("ck_tasks_priority_nonnegative", type_="check")
        batch_op.drop_constraint("ck_tasks_status_v2", type_="check")
        batch_op.drop_constraint(
            "fk_tasks_account_id_cloud_accounts",
            type_="foreignkey",
        )
        batch_op.drop_column("completed_at")
        batch_op.drop_column("last_error_message")
        batch_op.drop_column("last_error_code")
        batch_op.drop_column("lease_until")
        batch_op.drop_column("locked_at")
        batch_op.drop_column("lock_token")
        batch_op.drop_column("locked_by")
        batch_op.drop_column("cancel_requested_at")
        batch_op.drop_column("blocked_at")
        batch_op.drop_column("blocked_reason")
        batch_op.drop_column("max_retries")
        batch_op.drop_column("retry_count")
        batch_op.drop_column("payload")
        batch_op.drop_column("payload_version")
        batch_op.drop_column("priority")
        batch_op.drop_column("account_id")
