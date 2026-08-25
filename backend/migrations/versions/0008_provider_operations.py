"""Persist resumable provider write operations on tasks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_provider_operations"
down_revision: str | None = "0007_update_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column(
        "tasks", sa.Column("provider_write_intent_at", sa.DateTime(timezone=True))
    )
    op.add_column("tasks", sa.Column("provider_operation_id", sa.String(255)))
    op.add_column("tasks", sa.Column("provider_operation_status", sa.String(20)))
    op.add_column("tasks", sa.Column("provider_result", sa.JSON()))
    op.create_index(
        "ix_tasks_provider_operation",
        "tasks",
        ["provider_operation_status", "provider_operation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_provider_operation", table_name="tasks")
    op.drop_column("tasks", "provider_result")
    op.drop_column("tasks", "provider_operation_status")
    op.drop_column("tasks", "provider_operation_id")
    op.drop_column("tasks", "provider_write_intent_at")
