from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(slots=True)
class DriveRef:
    id: str
    type: str
    name: str


@dataclass(slots=True)
class AccountProfile:
    identity: str
    user_id: str | None = None
    default_drive_id: str | None = None
    drives: list[DriveRef] = field(default_factory=list)


@dataclass(slots=True)
class ShareInfo:
    share_key: str
    name: str
    root_folder_id: str = "root"


@dataclass(slots=True)
class RemoteItem:
    remote_file_id: str
    parent_id: str | None
    filename: str
    item_type: str
    size: int | None = None
    content_hash: str | None = None
    updated_at: datetime | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RemotePage:
    items: list[RemoteItem]
    next_marker: str | None = None


@dataclass(slots=True)
class FolderRef:
    folder_id: str
    path: str


@dataclass(slots=True)
class SaveResult:
    target_file_id: str
    target_path: str


class CloudDriveProvider(Protocol):
    async def validate_account(self) -> AccountProfile: ...

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo: ...

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage: ...

    async def resolve_target_path(self, path: str) -> FolderRef: ...

    async def list_target_items(
        self, target: FolderRef, marker: str | None = None
    ) -> RemotePage: ...

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef: ...

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None: ...

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult: ...

    def consume_refresh_token_update(self) -> str | None: ...
