from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse

import httpx

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderNotConfiguredError,
    ProviderRequestError,
)
from app.providers.baidu.open_write_client import BaiduOpenWriteClient
from app.providers.baidu.readonly_probe import _safe_int
from app.providers.baidu.write_client import normalize_target_path
from app.providers.base import (
    AccountProfile,
    DriveRef,
    FolderRef,
    RemoteItem,
    RemotePage,
    SaveResult,
    ShareInfo,
)

DEFAULT_OPENLIST_TOKEN_URL = "https://api.oplist.org/baiduyun/renewapi"
DEFAULT_ALISTGO_CLIENT_ID = "hq9yQ9w9kR4YHj1kyYafLygVocobh7Sf"
DEFAULT_ALISTGO_CLIENT_SECRET = "YH2VpZcFJHYNnV6vLfHQXDBhcE7ZChyE"
BAIDU_OAUTH_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
ROOT_FOLDER_ID = "/"


class BaiduOpenProvider:
    """Baidu account-drive adapter using hosted or direct OAuth refresh."""

    def __init__(
        self,
        refresh_token: str,
        *,
        oauth_token_url: str | None = DEFAULT_OPENLIST_TOKEN_URL,
        client_id: str = "",
        client_secret: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.refresh_token = refresh_token.strip()
        self.oauth_token_url = oauth_token_url.strip() if oauth_token_url else None
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self._http_client = http_client
        self._access_token: str | None = None
        self._refresh_token_update: str | None = None
        self.request_count = 0

    def _require_configuration(self) -> None:
        if not self.refresh_token:
            raise ProviderNotConfiguredError("Baidu OpenAPI refresh token is required")
        if self.oauth_token_url is None:
            if not self.client_id or not self.client_secret:
                raise ProviderNotConfiguredError(
                    "Baidu OpenAPI Client ID and Client Secret are required"
                )
            return
        parsed = urlparse(self.oauth_token_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderNotConfiguredError(
                "Baidu OpenAPI token URL must be HTTPS without credentials or query"
            )

    async def _ensure_access_token(self) -> None:
        if self._access_token:
            return
        self._require_configuration()
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=20)
        try:
            self.request_count += 1
            if self.oauth_token_url:
                response = await client.get(
                    self.oauth_token_url,
                    params={
                        "refresh_ui": self.refresh_token,
                        "server_use": "true",
                        "driver_txt": "baiduyun_go",
                    },
                )
            else:
                response = await client.get(
                    BAIDU_OAUTH_TOKEN_URL,
                    params={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderRequestError("Baidu OpenAPI token refresh request failed") from exc
        finally:
            if owns_client:
                await client.aclose()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError("Baidu OpenAPI token refresh returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("Baidu OpenAPI token refresh returned invalid data")
        access_token = str(payload.get("access_token") or "")
        rotated = str(payload.get("refresh_token") or "")
        if response.is_error or not access_token or not rotated:
            raise ProviderRequestError("Baidu OpenAPI token refresh failed")
        self._access_token = access_token
        if rotated != self.refresh_token:
            self.refresh_token = rotated
            self._refresh_token_update = rotated

    async def _client(self) -> BaiduOpenWriteClient:
        await self._ensure_access_token()
        assert self._access_token is not None
        return BaiduOpenWriteClient(self._access_token, http_client=self._http_client)

    async def _finish(self, client: BaiduOpenWriteClient) -> None:
        self.request_count += client.request_count
        await client.aclose()

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        timestamp = _safe_int(value)
        if timestamp is None or timestamp < 0:
            return None
        try:
            return datetime.fromtimestamp(timestamp, tz=UTC)
        except (OSError, OverflowError, ValueError):
            return None

    @classmethod
    def _item(cls, raw: dict[str, object]) -> RemoteItem:
        file_id = _safe_int(raw.get("fs_id"))
        name = raw.get("server_filename")
        isdir = _safe_int(raw.get("isdir"))
        path = raw.get("path")
        if file_id is None or file_id <= 0 or not isinstance(name, str) or not name:
            raise ProviderRequestError("Baidu OpenAPI item is missing its ID or name")
        if isdir not in {0, 1} or not isinstance(path, str):
            raise ProviderRequestError("Baidu OpenAPI item has an invalid type or path")
        size = _safe_int(raw.get("size"))
        return RemoteItem(
            remote_file_id=str(file_id),
            parent_id=str(PurePosixPath(path).parent),
            filename=name,
            item_type="folder" if isdir == 1 else "file",
            size=size if size is not None and size >= 0 else None,
            content_hash=str(raw.get("md5") or "") or None,
            updated_at=cls._parse_time(raw.get("server_mtime")),
            metadata={"path": path},
        )

    async def validate_account(self) -> AccountProfile:
        client = await self._client()
        try:
            payload = await client.fetch_account()
        finally:
            await self._finish(client)
        user_id = str(payload.get("uk") or "")
        if not user_id:
            raise ProviderRequestError("Baidu OpenAPI did not return a user ID")
        return AccountProfile(
            identity=str(payload.get("netdisk_name") or payload.get("baidu_name") or user_id),
            user_id=user_id,
            default_drive_id=ROOT_FOLDER_ID,
            drives=[DriveRef(ROOT_FOLDER_ID, "default", "默认盘")],
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        raise ProviderCapabilityError("Baidu OpenAPI does not expose share traversal")

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        raise ProviderCapabilityError("Baidu OpenAPI does not expose share traversal")

    @staticmethod
    def _offset(marker: str | None) -> int:
        if marker is None:
            return 0
        if not marker.isdecimal() or int(marker) > 10_000_000:
            raise ValueError("Invalid Baidu OpenAPI page marker")
        return int(marker)

    async def _list(self, path: str, marker: str | None = None) -> RemotePage:
        normalized = normalize_target_path(path)
        offset = self._offset(marker)
        client = await self._client()
        try:
            payload = await client._request(
                "directory listing",
                "/rest/2.0/xpan/file",
                params={
                    "method": "list",
                    "dir": normalized,
                    "start": offset,
                    "limit": 100,
                    "web": "web",
                    "order": "name",
                },
            )
        finally:
            await self._finish(client)
        raw_items = payload.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise ProviderRequestError("Baidu OpenAPI directory listing returned invalid items")
        return RemotePage(
            items=[self._item(item) for item in raw_items],
            next_marker=str(offset + 100) if len(raw_items) == 100 else None,
        )

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = normalize_target_path(path)
        if normalized == "/":
            return FolderRef(ROOT_FOLDER_ID, "/")
        parent = str(PurePosixPath(normalized).parent)
        name = PurePosixPath(normalized).name
        target = FolderRef(parent, parent)
        item = await self.find_target_item(target, name)
        if item is None or item.item_type != "folder":
            raise ProviderRequestError(f"Baidu OpenAPI folder does not exist: {normalized}")
        return FolderRef(normalized, normalized)

    async def list_target_items(self, target: FolderRef, marker: str | None = None) -> RemotePage:
        return await self._list(target.path, marker)

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("Invalid Baidu folder name")
        path = normalize_target_path(f"{parent.path.rstrip('/')}/{name}")
        existing = await self.find_target_item(parent, name)
        if existing is not None:
            if existing.item_type != "folder":
                raise ProviderRequestError(f"Baidu target exists and is not a folder: {name}")
            return FolderRef(path, path)
        client = await self._client()
        try:
            await client.create_folder(path)
        finally:
            await self._finish(client)
        return FolderRef(path, path)

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        marker: str | None = None
        while True:
            page = await self._list(target.path, marker)
            match = next((item for item in page.items if item.filename == name), None)
            if match is not None or page.next_marker is None:
                return match
            marker = page.next_marker

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        raise ProviderCapabilityError("Baidu OpenAPI does not expose share saving")

    def consume_refresh_token_update(self) -> str | None:
        value = self._refresh_token_update
        self._refresh_token_update = None
        return value
