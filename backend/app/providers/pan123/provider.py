from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from datetime import datetime

import httpx

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderRequestError,
    ProviderWriteUncertainError,
)
from app.providers.base import (
    AccountProfile,
    DriveRef,
    FolderRef,
    RemoteItem,
    RemotePage,
    SaveOperation,
    SaveResult,
    ShareInfo,
)
from app.providers.pan123.readonly_probe import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_PAGE_SIZE,
    Pan123ReadOnlyProbe,
    Pan123UpstreamChangedError,
    normalize_access_token,
    parse_share_url,
)
from app.providers.pan123.write_client import Pan123WriteClient

ROOT_FOLDER_ID = "0"


class Pan123PrivateProvider:
    """Experimental 123 Cloud Drive adapter for token-authenticated Web APIs."""

    def __init__(
        self,
        access_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        page_size: int = MAX_PAGE_SIZE,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
        login_uuid: str | None = None,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        self._access_token = normalize_access_token(access_token)
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._page_size = page_size
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._login_uuid = login_uuid or uuid.uuid4().hex
        self._share_passwords: dict[str, str] = {}
        self.request_count = 0

    def _new_probe(self, *, write: bool = False) -> Pan123ReadOnlyProbe:
        cls = Pan123WriteClient if write else Pan123ReadOnlyProbe
        return cls(
            self._access_token,
            http_client=self._http_client,
            timeout_seconds=self._timeout_seconds,
            max_retries=0 if write else self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )

    async def _finish_probe(self, probe: Pan123ReadOnlyProbe) -> None:
        self.request_count += probe.request_count
        await probe.aclose()

    @staticmethod
    def _page_number(marker: str | None) -> int:
        if marker is None:
            return 1
        if not marker.isdecimal() or not 1 <= int(marker) <= 1_000_000:
            raise ValueError("123 page marker must be a positive integer")
        return int(marker)

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def _to_remote_item(cls, raw: dict[str, object]) -> RemoteItem:
        raw_id = raw.get("FileId")
        name = raw.get("FileName")
        item_type = raw.get("Type")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)) or not str(raw_id):
            raise Pan123UpstreamChangedError("123 item contained no stable FileId")
        if not isinstance(name, str) or not name:
            raise Pan123UpstreamChangedError("123 item contained no FileName")
        if item_type not in {0, 1}:
            raise Pan123UpstreamChangedError("123 item contained no stable Type")
        parent_id = raw.get("ParentFileId")
        size = raw.get("Size")
        etag = raw.get("Etag")
        return RemoteItem(
            remote_file_id=str(raw_id),
            parent_id=(
                str(parent_id)
                if isinstance(parent_id, (int, str)) and not isinstance(parent_id, bool)
                else None
            ),
            filename=name,
            item_type="folder" if item_type == 1 else "file",
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            content_hash=etag if isinstance(etag, str) and etag else None,
            updated_at=cls._parse_time(raw.get("UpdateAt")),
            metadata={
                "etag": etag if isinstance(etag, str) else "",
                "type": item_type,
            },
        )

    @classmethod
    def _remote_page(
        cls, payload: dict[str, object], *, page: int, page_size: int
    ) -> RemotePage:
        data = payload.get("data")
        items = data.get("InfoList") if isinstance(data, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise Pan123UpstreamChangedError("123 listing contained invalid items")
        total = data.get("Total") if isinstance(data, dict) else None
        if isinstance(total, str) and total.isdecimal():
            total = int(total)
        total_count = total if isinstance(total, int) and not isinstance(total, bool) else None
        next_value = data.get("Next") if isinstance(data, dict) else None
        is_first = data.get("IsFirst") if isinstance(data, dict) else None
        has_more = (
            page * page_size < total_count
            if total_count is not None
            else next_value not in (None, "-1", -1) or is_first is False
        )
        return RemotePage(
            items=[cls._to_remote_item(item) for item in items],
            next_marker=str(page + 1) if has_more else None,
        )

    async def validate_account(self) -> AccountProfile:
        probe = self._new_probe()
        try:
            payload = await probe.fetch_account()
        finally:
            await self._finish_probe(probe)
        data = Pan123ReadOnlyProbe._data_object(payload, "account")
        user_id = data.get("UID")
        identity = data.get("Nickname")
        return AccountProfile(
            identity=str(identity or user_id or "123 Cloud Drive"),
            user_id=str(user_id) if isinstance(user_id, (int, str)) else None,
            default_drive_id=ROOT_FOLDER_ID,
            drives=[DriveRef(id=ROOT_FOLDER_ID, type="default", name="默认盘")],
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        share_key, url_password = parse_share_url(share_url)
        self._share_passwords[share_key] = password or url_password or ""
        return ShareInfo(share_key=share_key, name="123 share", root_folder_id=ROOT_FOLDER_ID)

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        if share.share_key not in self._share_passwords:
            raise ProviderRequestError("Resolve the 123 share before listing its items")
        page = self._page_number(marker)
        probe = self._new_probe()
        try:
            _key, _password, payload = await probe.fetch_share_page(
                f"https://www.123pan.com/s/{share.share_key}",
                share_password=self._share_passwords[share.share_key],
                parent_id=parent_id,
                page_size=self._page_size,
                page=page,
            )
        finally:
            await self._finish_probe(probe)
        return self._remote_page(payload, page=page, page_size=self._page_size)

    async def _list_drive_items(
        self, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        page = self._page_number(marker)
        probe = self._new_probe()
        try:
            payload = await probe.fetch_drive_page(
                parent_id, page_size=self._page_size, page=page
            )
        finally:
            await self._finish_probe(probe)
        return self._remote_page(payload, page=page, page_size=self._page_size)

    @staticmethod
    def _target_folder_id(folder_id: str) -> str:
        normalized = ROOT_FOLDER_ID if folder_id == "root" else folder_id
        if not normalized.isdecimal():
            raise ValueError("123 target folder ID must be numeric")
        return normalized

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        current = FolderRef(ROOT_FOLDER_ID, "/")
        for segment in (part for part in normalized.split("/") if part):
            match = await self.find_target_item(current, segment)
            if match is None or match.item_type != "folder":
                raise ProviderRequestError(f"123 folder does not exist: {normalized}")
            current = FolderRef(
                match.remote_file_id, f"{current.path.rstrip('/')}/{segment}"
            )
        return current

    async def list_target_items(
        self, target: FolderRef, marker: str | None = None
    ) -> RemotePage:
        return await self._list_drive_items(
            self._target_folder_id(target.folder_id), marker
        )

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        marker: str | None = None
        while True:
            page = await self.list_target_items(target, marker)
            match = next((item for item in page.items if item.filename == name), None)
            if match is not None or page.next_marker is None:
                return match
            marker = page.next_marker

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        existing = await self.find_target_item(parent, name)
        path = f"{parent.path.rstrip('/')}/{name}"
        if existing is not None:
            if existing.item_type != "folder":
                raise ProviderRequestError(f"123 target exists and is not a folder: {name}")
            return FolderRef(existing.remote_file_id, path)
        client = self._new_probe(write=True)
        assert isinstance(client, Pan123WriteClient)
        try:
            await client.create_folder(
                parent_folder_id=self._target_folder_id(parent.folder_id), name=name
            )
        finally:
            await self._finish_probe(client)
        for _attempt in range(5):
            created = await self.find_target_item(parent, name)
            if created is not None and created.item_type == "folder":
                return FolderRef(created.remote_file_id, path)
            await asyncio.sleep(0.5)
        raise ProviderWriteUncertainError(
            "123 folder creation was accepted but could not be verified"
        )

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        del share, source, target
        raise ProviderCapabilityError("123 share save requires the resumable write contract")

    async def _source_for_save(
        self, share: ShareInfo, source: RemoteItem
    ) -> RemoteItem:
        marker: str | None = None
        while True:
            page = await self.list_share_items(
                share, source.parent_id or share.root_folder_id, marker
            )
            match = next(
                (item for item in page.items if item.remote_file_id == source.remote_file_id),
                None,
            )
            if match is not None:
                return match
            if page.next_marker is None:
                raise ProviderRequestError("123 share item could not be found before save")
            marker = page.next_marker

    @staticmethod
    def _operation_fingerprint(item: RemoteItem) -> str:
        value = f"{item.filename}\0{item.size or 0}\0{item.content_hash or ''}\0{item.item_type}"
        return hashlib.sha256(value.encode()).hexdigest()[:24]

    @staticmethod
    def _operation_id(target_id: str, fingerprint: str) -> str:
        encoded_target = base64.urlsafe_b64encode(target_id.encode()).decode().rstrip("=")
        return f"p123v1.{encoded_target}.{fingerprint}"

    @staticmethod
    def _parse_operation_id(operation_id: str) -> tuple[str, str]:
        parts = operation_id.split(".")
        if len(parts) != 3 or parts[0] != "p123v1" or len(parts[2]) != 24:
            raise ValueError("Invalid 123 operation ID")
        try:
            target = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid 123 operation ID") from exc
        if not target.isdecimal() or not all(c in "0123456789abcdef" for c in parts[2]):
            raise ValueError("Invalid 123 operation ID")
        return target, parts[2]

    async def start_save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> str:
        if share.share_key not in self._share_passwords:
            raise ProviderRequestError("Resolve the 123 share before saving its items")
        resolved = await self._source_for_save(share, source)
        raw = {
            "FileId": int(resolved.remote_file_id),
            "FileName": resolved.filename,
            "Size": resolved.size or 0,
            "Type": 1 if resolved.item_type == "folder" else 0,
            "Etag": str(resolved.metadata.get("etag") or resolved.content_hash or ""),
        }
        client = self._new_probe(write=True)
        assert isinstance(client, Pan123WriteClient)
        try:
            if resolved.item_type == "file":
                await client.reuse_shared_file(
                    source=raw,
                    target_folder_id=self._target_folder_id(target.folder_id),
                    login_uuid=self._login_uuid,
                )
            else:
                await client.save_share_item(
                    share_key=share.share_key,
                    share_password=self._share_passwords[share.share_key],
                    source=raw,
                    target_folder_id=self._target_folder_id(target.folder_id),
                    login_uuid=self._login_uuid,
                )
        finally:
            await self._finish_probe(client)
        return self._operation_id(
            target.folder_id, self._operation_fingerprint(resolved)
        )

    async def query_save_operation(self, operation_id: str) -> SaveOperation:
        target_id, fingerprint = self._parse_operation_id(operation_id)
        marker: str | None = None
        target = FolderRef(target_id, "/")
        while True:
            page = await self.list_target_items(target, marker)
            match = next(
                (
                    item
                    for item in page.items
                    if self._operation_fingerprint(item) == fingerprint
                ),
                None,
            )
            if match is not None:
                return SaveOperation(operation_id, True, (match.remote_file_id,))
            if page.next_marker is None:
                return SaveOperation(operation_id, False)
            marker = page.next_marker

    def consume_refresh_token_update(self) -> str | None:
        return None
