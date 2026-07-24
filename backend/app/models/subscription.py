from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "cloud_account_id",
            "share_key",
            "source_folder_id",
            "target_folder_id",
            name="uq_subscription_source_target",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cloud_account_id: Mapped[int] = mapped_column(
        ForeignKey("cloud_accounts.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(150))
    provider: Mapped[str] = mapped_column(String(32), index=True)
    share_url: Mapped[str] = mapped_column(Text)
    share_key: Mapped[str | None] = mapped_column(String(255))
    share_password: Mapped[str | None] = mapped_column(Text)
    source_folder_id: Mapped[str | None] = mapped_column(String(255), default="root")
    target_path: Mapped[str] = mapped_column(Text)
    target_drive_id: Mapped[str | None] = mapped_column(String(128))
    target_drive_type: Mapped[str | None] = mapped_column(String(20))
    target_folder_id: Mapped[str | None] = mapped_column(String(255))
    schedule: Mapped[str] = mapped_column(String(100), default="interval:30m")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    initial_sync_mode: Mapped[str] = mapped_column(String(20), default="all")
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    cloud_account: Mapped["CloudAccount"] = relationship(back_populates="subscriptions")
    files: Mapped[list["CloudFile"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )
    folder_checkpoints: Mapped[list["FolderCheckpoint"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.file import CloudFile  # noqa: E402
from app.models.folder_checkpoint import FolderCheckpoint  # noqa: E402
from app.models.task import Task  # noqa: E402
