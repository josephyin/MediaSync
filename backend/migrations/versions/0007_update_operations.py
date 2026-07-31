"""Add persistent update operations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_update_operations"
down_revision: str | None = "0006_task_execution_data_model_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_SQL = (
    "'checking', 'available', 'pulling', 'draining', 'handoff', "
    "'snapshotting', 'switching', 'verifying', 'rolling_back', "
    "'success', 'failed', 'rolled_back', 'rollback_failed', 'cancelled'"
)


def upgrade() -> None:
    op.create_table(
        "update_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("active_slot", sa.String(16)),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("target_version", sa.String(64)),
        sa.Column("target_digest", sa.String(80)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_id", name="uq_update_operations_operation_id"),
        sa.CheckConstraint(
            f"status IN ({STATUS_SQL})",
            name="ck_update_operations_status",
        ),
        sa.CheckConstraint(
            "(active_slot = 'global' AND completed_at IS NULL) "
            "OR (active_slot IS NULL AND completed_at IS NOT NULL)",
            name="ck_update_operations_active_lifecycle",
        ),
    )
    op.create_index(
        "uq_update_operations_single_active",
        "update_operations",
        ["active_slot"],
        unique=True,
    )
    op.create_index(
        "ix_update_operations_status_created",
        "update_operations",
        ["status", "created_at"],
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_update_operations_terminal_immutable
            BEFORE UPDATE ON update_operations
            FOR EACH ROW
            WHEN OLD.active_slot IS NULL
            BEGIN
                SELECT RAISE(ABORT, 'terminal update operation is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_update_operations_no_delete
            BEFORE DELETE ON update_operations
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'update operation history cannot be deleted');
            END
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_update_operations_no_delete")
        op.execute(
            "DROP TRIGGER IF EXISTS trg_update_operations_terminal_immutable"
        )
    op.drop_index(
        "ix_update_operations_status_created",
        table_name="update_operations",
    )
    op.drop_index(
        "uq_update_operations_single_active",
        table_name="update_operations",
    )
    op.drop_table("update_operations")
