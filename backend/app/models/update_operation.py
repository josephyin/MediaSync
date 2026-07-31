from datetime import datetime

from sqlalchemy import DDL, CheckConstraint, DateTime, Index, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

UPDATE_OPERATION_STATUSES = (
    "checking",
    "available",
    "pulling",
    "draining",
    "handoff",
    "snapshotting",
    "switching",
    "verifying",
    "rolling_back",
    "success",
    "failed",
    "rolled_back",
    "rollback_failed",
    "cancelled",
)
TERMINAL_UPDATE_OPERATION_STATUSES = frozenset(
    {"success", "failed", "rolled_back", "rollback_failed", "cancelled"}
)


class UpdateOperation(TimestampMixin, Base):
    __tablename__ = "update_operations"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(item) for item in UPDATE_OPERATION_STATUSES)})",
            name="ck_update_operations_status",
        ),
        CheckConstraint(
            "(active_slot = 'global' AND completed_at IS NULL) "
            "OR (active_slot IS NULL AND completed_at IS NOT NULL)",
            name="ck_update_operations_active_lifecycle",
        ),
        Index(
            "uq_update_operations_single_active",
            "active_slot",
            unique=True,
        ),
        Index("ix_update_operations_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), unique=True)
    status: Mapped[str] = mapped_column(String(24))
    active_slot: Mapped[str | None] = mapped_column(String(16), default="global")
    source_version: Mapped[str] = mapped_column(String(64))
    target_version: Mapped[str | None] = mapped_column(String(64))
    target_digest: Mapped[str | None] = mapped_column(String(80))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


event.listen(
    UpdateOperation.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_update_operations_terminal_immutable
        BEFORE UPDATE ON update_operations
        FOR EACH ROW
        WHEN OLD.active_slot IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'terminal update operation is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    UpdateOperation.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_update_operations_no_delete
        BEFORE DELETE ON update_operations
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'update operation history cannot be deleted');
        END
        """
    ).execute_if(dialect="sqlite"),
)
