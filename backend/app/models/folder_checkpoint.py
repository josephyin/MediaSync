from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class FolderCheckpoint(TimestampMixin, Base):
    __tablename__ = "folder_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "remote_folder_id",
            name="uq_folder_checkpoint_subscription_remote",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    remote_folder_id: Mapped[str] = mapped_column(String(255))
    relative_path: Mapped[str] = mapped_column(Text)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    subscription: Mapped["Subscription"] = relationship(back_populates="folder_checkpoints")


from app.models.subscription import Subscription  # noqa: E402
