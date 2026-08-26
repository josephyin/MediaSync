from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CloudFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    subscription_id: int
    remote_file_id: str
    parent_remote_file_id: str | None
    filename: str
    relative_path: str
    item_type: str
    size: int | None
    content_hash: str | None
    fingerprint: str
    status: str
    target_file_id: str | None
    target_path: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    saved_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class BulkRetryFilesRead(BaseModel):
    matched_count: int
    enqueued_count: int
    skipped_count: int
