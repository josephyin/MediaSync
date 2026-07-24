"""Add deferred transfer retry timestamp."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task_retry_backoff"
down_revision: str | None = "0003_open_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
        batch_op.create_index("ix_tasks_next_attempt_at", ["next_attempt_at"])


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("ix_tasks_next_attempt_at")
        batch_op.drop_column("next_attempt_at")
