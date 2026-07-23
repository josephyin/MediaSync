from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CloudFile(TimestampMixin, Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("subscription_id", "remote_file_id", name="uq_file_subscription_remote"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    remote_file_id: Mapped[str] = mapped_column(String(255))
    parent_remote_file_id: Mapped[str | None] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255), index=True)
    relative_path: Mapped[str] = mapped_column(Text, default="")
    item_type: Mapped[str] = mapped_column(String(16), default="file")
    size: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="discovered", index=True)
    target_file_id: Mapped[str | None] = mapped_column(String(255))
    target_path: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    subscription: Mapped["Subscription"] = relationship(back_populates="files")
    tasks: Mapped[list["Task"]] = relationship(back_populates="file")


from app.models.subscription import Subscription  # noqa: E402
from app.models.task import Task  # noqa: E402
