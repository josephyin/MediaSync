from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SubscriptionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    cloud_account_id: int
    provider: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=32)
    share_url: str = Field(min_length=8)
    share_password: str | None = None
    source_folder_id: str | None = None
    target_path: str = Field(min_length=1)
    target_drive_id: str | None = Field(default=None, min_length=1, max_length=128)
    target_drive_type: str | None = Field(
        default=None, pattern="^(default|resource|backup|custom)$"
    )
    schedule: str = "interval:30m"
    initial_sync_mode: str = Field(default="all", pattern="^(all|future_only)$")
    enabled: bool = True

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, value: str) -> str:
        from app.scheduler.schedule import parse_schedule

        parse_schedule(value)
        return value


class SubscriptionUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    target_path: str | None = Field(default=None, min_length=1)
    target_drive_id: str | None = Field(default=None, min_length=1, max_length=128)
    target_drive_type: str | None = Field(
        default=None, pattern="^(default|resource|backup|custom)$"
    )
    schedule: str | None = None
    initial_sync_mode: str | None = Field(default=None, pattern="^(all|future_only)$")
    enabled: bool | None = None

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, value: str | None) -> str | None:
        if value is not None:
            from app.scheduler.schedule import parse_schedule

            parse_schedule(value)
        return value


class SubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    cloud_account_id: int
    name: str
    provider: str
    share_url: str
    share_key: str | None
    source_folder_id: str | None
    target_path: str
    target_drive_id: str | None
    target_drive_type: str | None
    target_folder_id: str | None
    schedule: str
    enabled: bool
    status: str
    initial_sync_mode: str
    last_scanned_at: datetime | None
    last_full_scanned_at: datetime | None
    next_scan_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class ShareResolveRequest(BaseModel):
    provider: str = "aliyundrive"
    share_url: str
    share_password: str | None = None


class ShareResolveResponse(BaseModel):
    share_key: str
    name: str
    root_folder_id: str
