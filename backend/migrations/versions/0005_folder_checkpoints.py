"""Add directory scan checkpoints."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_folder_checkpoints"
down_revision: str | None = "0004_task_retry_backoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.add_column(sa.Column("last_full_scanned_at", sa.DateTime(timezone=True)))

    op.create_table(
        "folder_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("remote_folder_id", sa.String(length=255), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "subscription_id",
            "remote_folder_id",
            name="uq_folder_checkpoint_subscription_remote",
        ),
    )
    op.create_index(
        "ix_folder_checkpoints_subscription_id",
        "folder_checkpoints",
        ["subscription_id"],
    )
    op.create_index(
        "ix_folder_checkpoints_last_scanned_at",
        "folder_checkpoints",
        ["last_scanned_at"],
    )
    op.create_index(
        "ix_folder_checkpoints_last_seen_at",
        "folder_checkpoints",
        ["last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_folder_checkpoints_last_seen_at",
        table_name="folder_checkpoints",
    )
    op.drop_index(
        "ix_folder_checkpoints_last_scanned_at",
        table_name="folder_checkpoints",
    )
    op.drop_index(
        "ix_folder_checkpoints_subscription_id",
        table_name="folder_checkpoints",
    )
    op.drop_table("folder_checkpoints")
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("last_full_scanned_at")
