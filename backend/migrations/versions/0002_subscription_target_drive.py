"""Add per-subscription target drive selection."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_subscription_target_drive"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("target_drive_id", sa.String(128)))
    op.add_column("subscriptions", sa.Column("target_drive_type", sa.String(20)))
    op.execute(
        """
        UPDATE subscriptions
        SET target_drive_id = (
            SELECT default_drive_id FROM cloud_accounts
            WHERE cloud_accounts.id = subscriptions.cloud_account_id
        ), target_drive_type = 'default'
        """
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "target_drive_type")
    op.drop_column("subscriptions", "target_drive_id")
