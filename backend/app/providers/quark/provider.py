from __future__ import annotations

from datetime import UTC, datetime
from typing import NoReturn

import httpx

from app.core.exceptions import ProviderCapabilityError, ProviderRequestError
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
from app.providers.quark.readonly_probe import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_PAGE_SIZE,
    QuarkReadOnlyProbe,
    QuarkUpstreamChangedError,
    parse_share_url,
)
from app.providers.quark.write_client import QuarkWriteClient

ROOT_FOLDER_ID = "0"


class QuarkPrivateProvider:
    """Experimental Quark Drive adapter for Cookie-authenticated Web APIs.

    Share tokens live only in this object. Allowlisted Cookie rotations can be
    handed to the account service for encrypted persistence. Resumable writes
    are exposed only through the persisted-operation contract.
    """

    def __init__(
        self,
        cookie: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        page_size: int = MAX_PAGE_SIZE,
        max_retries: int = 1,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        probe = QuarkReadOnlyProbe(
            cookie,
            http_client=http_client,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        self._cookie = probe.current_cookie
        self._original_cookie = self._cookie
        self._cookie_update: str | None = None
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._page_size = page_size
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._share_tokens: dict[str, str] = {}
        self.request_count = 0

    def _new_probe(self) -> QuarkReadOnlyProbe:
        return QuarkReadOnlyProbe(
            self._cookie,
            http_client=self._http_client,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )

    async def _finish_probe(self, probe: QuarkReadOnlyProbe) -> None:
        self._cookie = probe.current_cookie
        if self._cookie != self._original_cookie:
            self._cookie_update = self._cookie
        self.request_count += probe.request_count
        await probe.aclose()

    @staticmethod
    def _page_number(marker: str | None) -> int:
        if marker is None:
            return 1
        if not marker.isdecimal():
            raise ValueError("Quark Drive page marker must be a positive integer")
        page = int(marker)
        if not 1 <= page <= 1_000_000:
            raise ValueError("Quark Drive page marker is out of range")
        return page

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if not isinstance(value, int) or value < 0:
            return None
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    @classmethod
    def _to_remote_item(cls, raw: dict[str, object]) -> RemoteItem:
        file_id = raw.get("fid")
        filename = raw.get("file_name")
        if not isinstance(file_id, str) or not file_id:
            raise QuarkUpstreamChangedError("Quark item contained no stable file ID")
        if not isinstance(filename, str) or not filename:
            raise QuarkUpstreamChangedError("Quark item contained no file name")

        is_file = raw.get("file")
        is_directory = raw.get("dir")
        if isinstance(is_file, bool):
            item_type = "file" if is_file else "folder"
        elif isinstance(is_directory, bool):
            item_type = "folder" if is_directory else "file"
        else:
            raise QuarkUpstreamChangedError("Quark item contained no stable item type")

        parent_id = raw.get("pdir_fid")
        size = raw.get("size")
        return RemoteItem(
            remote_file_id=file_id,
            parent_id=parent_id if isinstance(parent_id, str) and parent_id else None,
            filename=filename,
            item_type=item_type,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            updated_at=cls._parse_time(raw.get("updated_at")),
            metadata=(
                {"share_fid_token": raw["share_fid_token"]}
                if isinstance(raw.get("share_fid_token"), str)
                and raw["share_fid_token"]
                else {}
            ),
        )

    @classmethod
    def _remote_page(
        cls,
        payload: dict[str, object],
        *,
        page: int,
        page_size: int,
        operation: str,
    ) -> RemotePage:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QuarkUpstreamChangedError(f"Quark {operation} contained no data object")
        raw_items = data.get("list")
        if not isinstance(raw_items, list) or any(
            not isinstance(item, dict) for item in raw_items
        ):
            raise QuarkUpstreamChangedError(f"Quark {operation} contained invalid items")

        metadata = payload.get("metadata")
        raw_total = metadata.get("_total") if isinstance(metadata, dict) else None
        if isinstance(raw_total, str) and raw_total.isdecimal():
            raw_total = int(raw_total)
        total = (
            raw_total
            if isinstance(raw_total, int) and not isinstance(raw_total, bool)
            else None
        )
        has_more = page * page_size < total if total is not None else len(raw_items) == page_size
        return RemotePage(
            items=[cls._to_remote_item(item) for item in raw_items],
            next_marker=str(page + 1) if has_more else None,
        )

    async def validate_account(self) -> AccountProfile:
        probe = self._new_probe()
        try:
            data = await probe.fetch_account()
            if not any(
                field in data for field in ("member_type", "member_status", "total_capacity")
            ):
                raise QuarkUpstreamChangedError(
                    "Quark account response contained no stable membership field"
                )
        finally:
            await self._finish_probe(probe)
        user_id = str(data.get("user_id") or "") or None
        return AccountProfile(
            identity=str(data.get("nickname") or user_id or "Quark Drive"),
            user_id=user_id,
            default_drive_id=ROOT_FOLDER_ID,
            drives=[DriveRef(id=ROOT_FOLDER_ID, type="default", name="默认盘")],
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        share_id = parse_share_url(share_url)
        probe = self._new_probe()
        try:
            self._share_tokens[share_id] = await probe.fetch_share_token(
                share_id, password or ""
            )
        finally:
            await self._finish_probe(probe)
        return ShareInfo(
            share_key=share_id,
            name="Quark share",
            root_folder_id=ROOT_FOLDER_ID,
        )

    async def list_share_items(
        self,
        share: ShareInfo,
        parent_id: str,
        marker: str | None = None,
    ) -> RemotePage:
        stoken = self._share_tokens.get(share.share_key)
        if stoken is None:
            raise ProviderRequestError("Resolve the Quark share before listing its items")
        page = self._page_number(marker)
        probe = self._new_probe()
        try:
            payload = await probe.fetch_share_page(
                share.share_key,
                stoken,
                parent_id,
                page=page,
                page_size=self._page_size,
            )
        finally:
            await self._finish_probe(probe)
        return self._remote_page(
            payload,
            page=page,
            page_size=self._page_size,
            operation="share listing",
        )

    async def _list_drive_items(
        self, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        page = self._page_number(marker)
        probe = self._new_probe()
        try:
            payload = await probe.fetch_drive_page(
                parent_id,
                page=page,
                page_size=self._page_size,
            )
        finally:
            await self._finish_probe(probe)
        return self._remote_page(
            payload,
            page=page,
            page_size=self._page_size,
            operation="drive listing",
        )

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        current = FolderRef(folder_id=ROOT_FOLDER_ID, path="/")
        for segment in (part for part in normalized.split("/") if part):
            marker: str | None = None
            match: RemoteItem | None = None
            while True:
                page = await self._list_drive_items(current.folder_id, marker)
                match = next(
                    (
                        item
                        for item in page.items
                        if item.item_type == "folder" and item.filename == segment
                    ),
                    None,
                )
                if match is not None or page.next_marker is None:
                    break
                marker = page.next_marker
            if match is None:
                raise ProviderRequestError(f"Quark Drive folder does not exist: {normalized}")
            current = FolderRef(
                folder_id=match.remote_file_id,
                path=f"{current.path.rstrip('/')}/{segment}",
            )
        return current

    async def list_target_items(
        self, target: FolderRef, marker: str | None = None
    ) -> RemotePage:
        return await self._list_drive_items(target.folder_id, marker)

    @staticmethod
    def _write_unavailable(operation: str) -> NoReturn:
        raise ProviderCapabilityError(
            f"Quark Drive {operation} requires the resumable write contract"
        )

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        existing = await self.find_target_item(parent, name)
        if existing is not None:
            if existing.item_type != "folder":
                raise ProviderRequestError(
                    f"Quark Drive target already exists and is not a folder: {name}"
                )
            return FolderRef(
                folder_id=existing.remote_file_id,
                path=f"{parent.path.rstrip('/')}/{name}",
            )
        client = QuarkWriteClient(
            self._cookie,
            http_client=self._http_client,
            timeout_seconds=self._timeout_seconds,
            max_retries=0,
        )
        try:
            folder_id = await client.create_folder(parent.folder_id, name)
        finally:
            await self._finish_probe(client)
        return FolderRef(
            folder_id=folder_id,
            path=f"{parent.path.rstrip('/')}/{name}",
        )

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        marker: str | None = None
        while True:
            page = await self.list_target_items(target, marker)
            match = next((item for item in page.items if item.filename == name), None)
            if match is not None or page.next_marker is None:
                return match
            marker = page.next_marker

    async def save_shared_item(
        self,
        share: ShareInfo,
        source: RemoteItem,
        target: FolderRef,
    ) -> SaveResult:
        del share, source, target
        self._write_unavailable("share save")

    async def start_save_shared_item(
        self,
        share: ShareInfo,
        source: RemoteItem,
        target: FolderRef,
    ) -> str:
        stoken = self._share_tokens.get(share.share_key)
        source_token = source.metadata.get("share_fid_token")
        if stoken is None:
            raise ProviderRequestError("Resolve the Quark share before saving its items")
        if not isinstance(source_token, str) or not source_token:
            marker: str | None = None
            while True:
                page = await self.list_share_items(
                    share,
                    source.parent_id or share.root_folder_id,
                    marker,
                )
                match = next(
                    (
                        item
                        for item in page.items
                        if item.remote_file_id == source.remote_file_id
                    ),
                    None,
                )
                if match is not None:
                    source_token = match.metadata.get("share_fid_token")
                    break
                if page.next_marker is None:
                    break
                marker = page.next_marker
        if not isinstance(source_token, str) or not source_token:
            raise ProviderRequestError(
                "Quark share item is missing its save authorization token"
            )
        client = QuarkWriteClient(
            self._cookie,
            http_client=self._http_client,
            timeout_seconds=self._timeout_seconds,
            max_retries=0,
        )
        try:
            return await client.start_share_save(
                share_id=share.share_key,
                share_token=stoken,
                source_id=source.remote_file_id,
                source_token=source_token,
                target_folder_id=target.folder_id,
            )
        finally:
            await self._finish_probe(client)

    async def query_save_operation(self, operation_id: str) -> SaveOperation:
        client = QuarkWriteClient(
            self._cookie,
            http_client=self._http_client,
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            retry_backoff_seconds=self._retry_backoff_seconds,
        )
        try:
            return await client.query_save_task(operation_id)
        finally:
            await self._finish_probe(client)

    def consume_refresh_token_update(self) -> str | None:
        value = self._cookie_update
        self._cookie_update = None
        if value is not None:
            self._original_cookie = value
        return value
