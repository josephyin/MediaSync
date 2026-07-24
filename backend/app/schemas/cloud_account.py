from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CloudAccountBase(BaseModel):
    provider: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=32)
    name: str = Field(min_length=1, max_length=100)


class CloudAccountCreate(CloudAccountBase):
    refresh_token: str = Field(min_length=1)


class CloudAccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    refresh_token: str | None = Field(default=None, min_length=1)
    status: str | None = Field(default=None, pattern="^(active|expired|error|disabled)$")


class OpenCredentialConfigure(BaseModel):
    mode: str = Field(pattern="^(alistgo|openlist|custom)$")
    refresh_token: str | None = Field(default=None, min_length=1)
    token_url: str | None = Field(default=None, min_length=8, max_length=500)
    client_id: str | None = Field(default=None, min_length=1, max_length=255)
    client_secret: str | None = Field(default=None, min_length=1)


class QrLoginStartRequest(BaseModel):
    account_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)


class QrLoginStartResponse(BaseModel):
    session_id: str
    qr_code_data_url: str
    expires_in: int


class QrLoginStatusResponse(BaseModel):
    status: str
    account: "CloudAccountRead | None" = None


class CloudAccountRead(CloudAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_identity: str | None
    provider_user_id: str | None
    default_drive_id: str | None
    status: str
    last_verified_at: datetime | None
    last_error: str | None
    open_auth_mode: str | None
    open_account_identity: str | None
    open_status: str | None
    open_last_verified_at: datetime | None
    open_last_error: str | None
    open_token_url: str | None
    open_client_id: str | None
    created_at: datetime
    updated_at: datetime


class DriveInfo(BaseModel):
    id: str
    name: str
    type: str


class FolderInfo(BaseModel):
    id: str
    name: str
    path: str


class FolderItem(BaseModel):
    id: str
    name: str
    type: str
    size: int | None
    updated_at: datetime | None
