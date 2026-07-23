from datetime import datetime

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class CloudAccount(TimestampMixin, Base):
    __tablename__ = "cloud_accounts"
    __table_args__ = (UniqueConstraint("provider", "name", name="uq_account_provider_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(100))
    refresh_token: Mapped[str] = mapped_column(Text)
    account_identity: Mapped[str | None] = mapped_column(String(255))
    provider_user_id: Mapped[str | None] = mapped_column(String(128))
    default_drive_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    open_auth_mode: Mapped[str | None] = mapped_column(String(20))
    open_refresh_token: Mapped[str | None] = mapped_column(Text)
    open_client_id: Mapped[str | None] = mapped_column(String(255))
    open_client_secret: Mapped[str | None] = mapped_column(Text)
    open_token_url: Mapped[str | None] = mapped_column(Text)
    open_account_identity: Mapped[str | None] = mapped_column(String(255))
    open_status: Mapped[str | None] = mapped_column(String(20))
    open_last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_last_error: Mapped[str | None] = mapped_column(Text)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="cloud_account")


from app.models.subscription import Subscription  # noqa: E402
