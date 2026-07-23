"""Add optional Aliyun Drive OpenAPI credentials."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_open_credentials"
down_revision: str | None = "0002_subscription_target_drive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cloud_accounts", sa.Column("provider_user_id", sa.String(128)))
    op.add_column("cloud_accounts", sa.Column("open_auth_mode", sa.String(20)))
    op.add_column("cloud_accounts", sa.Column("open_refresh_token", sa.Text()))
    op.add_column("cloud_accounts", sa.Column("open_client_id", sa.String(255)))
    op.add_column("cloud_accounts", sa.Column("open_client_secret", sa.Text()))
    op.add_column("cloud_accounts", sa.Column("open_token_url", sa.Text()))
    op.add_column("cloud_accounts", sa.Column("open_account_identity", sa.String(255)))
    op.add_column("cloud_accounts", sa.Column("open_status", sa.String(20)))
    op.add_column(
        "cloud_accounts", sa.Column("open_last_verified_at", sa.DateTime(timezone=True))
    )
    op.add_column("cloud_accounts", sa.Column("open_last_error", sa.Text()))


def downgrade() -> None:
    op.drop_column("cloud_accounts", "open_last_error")
    op.drop_column("cloud_accounts", "open_last_verified_at")
    op.drop_column("cloud_accounts", "open_status")
    op.drop_column("cloud_accounts", "open_account_identity")
    op.drop_column("cloud_accounts", "open_token_url")
    op.drop_column("cloud_accounts", "open_client_secret")
    op.drop_column("cloud_accounts", "open_client_id")
    op.drop_column("cloud_accounts", "open_refresh_token")
    op.drop_column("cloud_accounts", "open_auth_mode")
    op.drop_column("cloud_accounts", "provider_user_id")
