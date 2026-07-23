from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

TASK_STATUSES = (
    "pending",
    "running",
    "retry",
    "waiting_credential",
    "cancel_requested",
    "success",
    "failed",
    "cancelled",
)
TASK_RUN_STATUSES = ("running", "success", "failed", "blocked", "lost", "cancelled")
TERMINAL_TASK_RUN_STATUSES = frozenset(
    {"success", "failed", "blocked", "lost", "cancelled"}
)


class Task(TimestampMixin, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in TASK_STATUSES)})",
            name="ck_tasks_status_v2",
        ),
        CheckConstraint("priority >= 0", name="ck_tasks_priority_nonnegative"),
        CheckConstraint("payload_version >= 1", name="ck_tasks_payload_version_positive"),
        CheckConstraint("retry_count >= 0", name="ck_tasks_retry_count_nonnegative"),
        CheckConstraint("max_retries >= 0", name="ck_tasks_max_retries_nonnegative"),
        CheckConstraint(
            """
            (
                locked_by IS NULL
                AND lock_token IS NULL
                AND locked_at IS NULL
                AND lease_until IS NULL
            )
            OR
            (
                locked_by IS NOT NULL
                AND lock_token IS NOT NULL
                AND locked_at IS NOT NULL
                AND lease_until IS NOT NULL
            )
            """,
            name="ck_tasks_ownership_fields_complete",
        ),
        Index(
            "ix_tasks_queue",
            "status",
            "next_attempt_at",
            "priority",
            "created_at",
        ),
        Index("ix_tasks_account_status", "account_id", "status"),
        Index("ix_tasks_lease_recovery", "status", "lease_until"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("cloud_accounts.id", ondelete="RESTRICT")
    )
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32), index=True)
    trigger_type: Mapped[str] = mapped_column(String(20), default="scheduled")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    payload_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(100))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    lock_token: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # v0.1 compatibility fields remain until execution moves to Task Engine v2.
    message: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped["CloudAccount | None"] = relationship()
    subscription: Mapped["Subscription | None"] = relationship(back_populates="tasks")
    file: Mapped["CloudFile | None"] = relationship(back_populates="tasks")
    runs: Mapped[list["TaskRun"]] = relationship(
        back_populates="task",
        order_by="TaskRun.run_number",
        passive_deletes=True,
    )


class TaskRun(TimestampMixin, Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "run_number", name="uq_task_runs_task_run_number"),
        CheckConstraint(
            f"status IN ({', '.join(repr(status) for status in TASK_RUN_STATUSES)})",
            name="ck_task_runs_status",
        ),
        CheckConstraint("run_number >= 1", name="ck_task_runs_run_number_positive"),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_task_runs_duration_nonnegative",
        ),
        Index("ix_task_runs_task_created", "task_id", "created_at"),
        Index("ix_task_runs_status_started", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"), index=True
    )
    run_number: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lock_token: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    result_summary: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)

    task: Mapped[Task] = relationship(back_populates="runs")


event.listen(
    Task.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_tasks_payload_immutable
        BEFORE UPDATE OF payload, payload_version ON tasks
        FOR EACH ROW
        WHEN NEW.payload IS NOT OLD.payload
            OR NEW.payload_version IS NOT OLD.payload_version
        BEGIN
            SELECT RAISE(ABORT, 'task payload and version are immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    Task.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_tasks_idempotency_key_immutable
        BEFORE UPDATE OF idempotency_key ON tasks
        FOR EACH ROW
        WHEN OLD.idempotency_key IS NOT NULL
            AND NEW.idempotency_key IS NOT OLD.idempotency_key
        BEGIN
            SELECT RAISE(ABORT, 'task idempotency key is immutable once assigned');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    TaskRun.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_task_runs_identity_immutable
        BEFORE UPDATE OF task_id, run_number ON task_runs
        FOR EACH ROW
        WHEN NEW.task_id IS NOT OLD.task_id
            OR NEW.run_number IS NOT OLD.run_number
        BEGIN
            SELECT RAISE(ABORT, 'task run identity is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    TaskRun.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_task_runs_terminal_immutable
        BEFORE UPDATE ON task_runs
        FOR EACH ROW
        WHEN OLD.status IN ('success', 'failed', 'blocked', 'lost', 'cancelled')
        BEGIN
            SELECT RAISE(ABORT, 'terminal task run is immutable');
        END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    TaskRun.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER trg_task_runs_no_delete
        BEFORE DELETE ON task_runs
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'task run history cannot be deleted');
        END
        """
    ).execute_if(dialect="sqlite"),
)


from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.file import CloudFile  # noqa: E402
from app.models.subscription import Subscription  # noqa: E402
