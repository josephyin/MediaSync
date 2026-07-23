"""Create MediaSync MVP tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "cloud_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("account_identity", sa.String(255)),
        sa.Column("default_drive_id", sa.String(128)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *timestamps(),
        sa.UniqueConstraint("provider", "name", name="uq_account_provider_name"),
    )
    op.create_index("ix_cloud_accounts_provider", "cloud_accounts", ["provider"])
    op.create_index("ix_cloud_accounts_status", "cloud_accounts", ["status"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cloud_account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("share_url", sa.Text(), nullable=False),
        sa.Column("share_key", sa.String(255)),
        sa.Column("share_password", sa.Text()),
        sa.Column("source_folder_id", sa.String(255)),
        sa.Column("target_path", sa.Text(), nullable=False),
        sa.Column("target_folder_id", sa.String(255)),
        sa.Column("schedule", sa.String(100), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("initial_sync_mode", sa.String(20), nullable=False),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True)),
        sa.Column("next_scan_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *timestamps(),
        sa.ForeignKeyConstraint(["cloud_account_id"], ["cloud_accounts.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "cloud_account_id",
            "share_key",
            "source_folder_id",
            "target_folder_id",
            name="uq_subscription_source_target",
        ),
    )
    op.create_index("ix_subscriptions_cloud_account_id", "subscriptions", ["cloud_account_id"])
    op.create_index("ix_subscriptions_enabled", "subscriptions", ["enabled"])
    op.create_index("ix_subscriptions_provider", "subscriptions", ["provider"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("remote_file_id", sa.String(255), nullable=False),
        sa.Column("parent_remote_file_id", sa.String(255)),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("item_type", sa.String(16), nullable=False),
        sa.Column("size", sa.BigInteger()),
        sa.Column("content_hash", sa.String(255)),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("target_file_id", sa.String(255)),
        sa.Column("target_path", sa.Text()),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        *timestamps(),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "subscription_id", "remote_file_id", name="uq_file_subscription_remote"
        ),
    )
    op.create_index("ix_files_filename", "files", ["filename"])
    op.create_index("ix_files_status", "files", ["status"])
    op.create_index("ix_files_subscription_id", "files", ["subscription_id"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer()),
        sa.Column("file_id", sa.Integer()),
        sa.Column("type", sa.String(32), nullable=False),
        sa.Column("trigger_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(255), unique=True),
        sa.Column("message", sa.Text()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_tasks_file_id", "tasks", ["file_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_subscription_id", "tasks", ["subscription_id"])
    op.create_index("ix_tasks_type", "tasks", ["type"])


def downgrade() -> None:
    op.drop_table("tasks")
    op.drop_table("files")
    op.drop_table("subscriptions")
    op.drop_table("cloud_accounts")
