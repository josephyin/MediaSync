from datetime import datetime
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.core.exceptions import ProviderRequestError
from app.providers.aliyundrive.request_guard import request_guard
from app.providers.base import (
    AccountProfile,
    DriveRef,
    FolderRef,
    RemoteItem,
    RemotePage,
    SaveResult,
    ShareInfo,
)


class AliyunDrivePrivateProvider:
    """Experimental adapter for Aliyun Drive Web private APIs.

    These endpoints are not a stable public contract. Keeping this adapter
    separate from the official OpenAPI implementation limits the impact of
    upstream changes and makes the selected mode explicit in configuration.
    """

    def __init__(
        self,
        refresh_token: str,
        api_base_url: str = "https://api.alipan.com",
        auth_base_url: str = "https://auth.alipan.com",
        drive_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        request_interval_seconds: float | None = None,
        retry_backoff_seconds: float | None = None,
    ):
        settings = get_settings()
        self.refresh_token = refresh_token
        self.api_base_url = api_base_url.rstrip("/")
        self.auth_base_url = auth_base_url.rstrip("/")
        self._http_client = http_client
        self._access_token: str | None = None
        self._refresh_token_update: str | None = None
        self._drive_id: str | None = drive_id
        self._selected_drive_id = drive_id
        self._drives: list[DriveRef] = []
        self._identity: str | None = None
        self._user_id: str | None = None
        self._share_tokens: dict[str, str] = {}
        self._share_passwords: dict[str, str | None] = {}
        self._request_interval_seconds = (
            settings.aliyundrive_request_interval_seconds
            if request_interval_seconds is None
            else request_interval_seconds
        )
        self._request_jitter_seconds = (
            0 if request_interval_seconds == 0 else settings.aliyundrive_request_jitter_seconds
        )
        self._request_max_retries = settings.aliyundrive_request_max_retries
        self._retry_backoff_seconds = (
            settings.aliyundrive_retry_backoff_seconds
            if retry_backoff_seconds is None
            else retry_backoff_seconds
        )
        self._retry_max_seconds = settings.aliyundrive_retry_max_seconds
        self.request_count = 0

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
        size = raw.get("size")
        return RemoteItem(
            remote_file_id=str(raw.get("file_id", "")),
            parent_id=str(raw.get("parent_file_id", "")) or None,
            filename=str(raw.get("name") or raw.get("file_name") or ""),
            item_type=str(raw.get("type", "file")),
            size=int(size) if size is not None else None,
            content_hash=str(raw.get("content_hash", "")) or None,
            updated_at=cls._parse_time(raw.get("updated_at")),
            metadata={"drive_id": raw.get("drive_id")},
        )

    @staticmethod
    def _message(payload: dict[str, object], fallback: str) -> str:
        return str(payload.get("message") or payload.get("display_message") or fallback)

    async def _post_url(
        self,
        url: str,
        body: dict[str, object],
        *,
        authenticated: bool = False,
        share_token: str | None = None,
        retryable: bool = True,
    ) -> dict[str, object]:
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://www.alipan.com",
            "Referer": "https://www.alipan.com/",
            "X-Canary": "client=web,app=share,version=v2.3.1",
        }
        if authenticated:
            await self._ensure_access_token()
            headers["Authorization"] = f"Bearer {self._access_token}"
        if share_token:
            headers["x-share-token"] = share_token

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=20)
        try:
            response: httpx.Response | None = None
            max_retries = self._request_max_retries if retryable else 0
            for attempt in range(max_retries + 1):
                async with request_guard.slot(
                    self._request_interval_seconds,
                    self._request_jitter_seconds,
                ):
                    self.request_count += 1
                    response = await client.post(url, json=body, headers=headers)
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
                if attempt >= max_retries:
                    break
                retry_after_value = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_value) if retry_after_value else None
                except ValueError:
                    retry_after = None
                await request_guard.backoff(
                    attempt,
                    self._retry_backoff_seconds,
                    self._retry_max_seconds,
                    retry_after,
                )
        except httpx.HTTPError as exc:
            raise ProviderRequestError(f"Aliyun Drive private API request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response is None:
            raise ProviderRequestError("Aliyun Drive private API request was not executed")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Aliyun Drive private API returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("Aliyun Drive private API returned an invalid response")
        if response.is_error or payload.get("code"):
            code = payload.get("code", response.status_code)
            message = self._message(payload, response.reason_phrase)
            raise ProviderRequestError(f"Aliyun Drive private API {code}: {message}")
        return payload

    async def _api_post(
        self,
        path: str,
        body: dict[str, object],
        *,
        authenticated: bool = True,
        share_token: str | None = None,
        retryable: bool = True,
    ) -> dict[str, object]:
        return await self._post_url(
            f"{self.api_base_url}{path}",
            body,
            authenticated=authenticated,
            share_token=share_token,
            retryable=retryable,
        )

    async def _ensure_access_token(self) -> None:
        if self._access_token:
            return
        payload = await self._post_url(
            f"{self.auth_base_url}/v2/account/token",
            {"refresh_token": self.refresh_token, "grant_type": "refresh_token"},
        )
        access_token = str(payload.get("access_token", ""))
        rotated_refresh_token = str(payload.get("refresh_token", ""))
        if not access_token:
            raise ProviderRequestError("Aliyun Drive token response contained no access token")
        self._access_token = access_token
        if rotated_refresh_token and rotated_refresh_token != self.refresh_token:
            self.refresh_token = rotated_refresh_token
            self._refresh_token_update = rotated_refresh_token
        self._remember_account_fields(payload)

    def _remember_account_fields(self, payload: dict[str, object]) -> None:
        labels = {
            "default": "默认盘",
            "resource": "资源库",
            "backup": "备份盘",
        }
        drives: list[DriveRef] = []
        seen: set[str] = set()
        for drive_type in ("default", "resource", "backup"):
            drive_id = str(payload.get(f"{drive_type}_drive_id") or "")
            if drive_id and drive_id not in seen:
                drives.append(DriveRef(id=drive_id, type=drive_type, name=labels[drive_type]))
                seen.add(drive_id)
        if drives:
            self._drives = drives
        if not self._selected_drive_id:
            preferred = next((item for item in drives if item.type == "resource"), None)
            selected = preferred or (drives[0] if drives else None)
            if selected:
                self._drive_id = selected.id
        identity = str(
            payload.get("user_name") or payload.get("nick_name") or payload.get("user_id") or ""
        )
        if identity:
            self._identity = identity
        user_id = str(payload.get("user_id") or "")
        if user_id:
            self._user_id = user_id

    async def _load_account(self) -> None:
        payload = await self._api_post("/v2/user/get", {})
        self._remember_account_fields(payload)
        if not self._drive_id:
            raise ProviderRequestError("Aliyun Drive did not return an available drive ID")

    async def _get_drive_id(self) -> str:
        if not self._drive_id:
            await self._load_account()
        return str(self._drive_id)

    async def validate_account(self) -> AccountProfile:
        await self._load_account()
        return AccountProfile(
            identity=self._identity or "Aliyun Drive",
            user_id=self._user_id,
            default_drive_id=self._drive_id,
            drives=self._drives,
        )

    @staticmethod
    def _parse_share_url(share_url: str) -> tuple[str, str]:
        parsed = urlparse(share_url)
        allowed_hosts = {
            "www.alipan.com",
            "alipan.com",
            "www.aliyundrive.com",
            "aliyundrive.com",
        }
        if parsed.hostname not in allowed_hosts:
            raise ValueError("Share URL must use an Aliyun Drive domain")
        parts = [part for part in parsed.path.split("/") if part]
        prefix = next((index for index, part in enumerate(parts) if part in {"s", "share"}), -1)
        if prefix < 0 or prefix + 1 >= len(parts):
            raise ValueError("Invalid Aliyun Drive share URL")
        share_key = parts[prefix + 1]
        root_folder_id = "root"
        if "folder" in parts:
            folder_index = parts.index("folder")
            if folder_index + 1 < len(parts):
                root_folder_id = parts[folder_index + 1]
        return share_key, root_folder_id

    async def _get_share_token(self, share_key: str) -> str:
        cached = self._share_tokens.get(share_key)
        if cached:
            return cached
        payload = await self._api_post(
            "/v2/share_link/get_share_token",
            {
                "share_id": share_key,
                "share_pwd": self._share_passwords.get(share_key) or "",
            },
            authenticated=False,
        )
        share_token = str(payload.get("share_token", ""))
        if not share_token:
            raise ProviderRequestError("Aliyun Drive share response contained no share token")
        self._share_tokens[share_key] = share_token
        return share_token

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        share_key, root_folder_id = self._parse_share_url(share_url)
        self._share_passwords[share_key] = password
        await self._get_share_token(share_key)
        return ShareInfo(
            share_key=share_key,
            name=f"Aliyun share {share_key}",
            root_folder_id=root_folder_id,
        )

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        payload = await self._api_post(
            "/adrive/v3/file/list",
            {
                "share_id": share.share_key,
                "parent_file_id": parent_id,
                "limit": 200,
                "marker": marker or "",
                "order_by": "name",
                "order_direction": "ASC",
                "fields": "*",
            },
            authenticated=False,
            share_token=await self._get_share_token(share.share_key),
        )
        return self._remote_page(payload, "share file list")

    @classmethod
    def _remote_page(cls, payload: dict[str, object], operation: str) -> RemotePage:
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise ProviderRequestError(f"Aliyun Drive {operation} returned invalid items")
        return RemotePage(
            items=[cls._to_remote_item(item) for item in raw_items if isinstance(item, dict)],
            next_marker=str(payload.get("next_marker", "")) or None,
        )

    async def _list_drive_items(self, parent_id: str, marker: str | None = None) -> RemotePage:
        payload = await self._api_post(
            "/v2/file/list",
            {
                "drive_id": await self._get_drive_id(),
                "parent_file_id": parent_id,
                "limit": 200,
                "marker": marker or "",
                "order_by": "name",
                "order_direction": "ASC",
                "fields": "*",
            },
        )
        return self._remote_page(payload, "file list")

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        current = FolderRef(folder_id="root", path="/")
        await self._get_drive_id()
        for part in [segment for segment in normalized.split("/") if segment]:
            marker: str | None = None
            match: RemoteItem | None = None
            while True:
                page = await self._list_drive_items(current.folder_id, marker)
                match = next(
                    (
                        item
                        for item in page.items
                        if item.item_type == "folder" and item.filename == part
                    ),
                    None,
                )
                if match or not page.next_marker:
                    break
                marker = page.next_marker
            if match is None:
                raise ProviderRequestError(
                    f"Aliyun Drive target folder does not exist: {normalized}"
                )
            current = FolderRef(
                folder_id=match.remote_file_id,
                path=f"{current.path.rstrip('/')}/{part}",
            )
        return current

    async def list_target_items(self, target: FolderRef, marker: str | None = None) -> RemotePage:
        return await self._list_drive_items(target.folder_id, marker)

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        marker: str | None = None
        while True:
            page = await self._list_drive_items(parent.folder_id, marker)
            existing = next(
                (
                    item
                    for item in page.items
                    if item.item_type == "folder" and item.filename == name
                ),
                None,
            )
            if existing:
                return FolderRef(
                    existing.remote_file_id,
                    f"{parent.path.rstrip('/')}/{name}",
                )
            if not page.next_marker:
                break
            marker = page.next_marker
        payload = await self._api_post(
            "/adrive/v2/file/createWithFolders",
            {
                "drive_id": await self._get_drive_id(),
                "parent_file_id": parent.folder_id,
                "name": name,
                "type": "folder",
                "check_name_mode": "refuse",
            },
            retryable=False,
        )
        folder_id = str(payload.get("file_id", ""))
        if not folder_id:
            raise ProviderRequestError("Aliyun Drive folder creation returned no file ID")
        return FolderRef(folder_id, f"{parent.path.rstrip('/')}/{name}")

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        marker: str | None = None
        while True:
            page = await self._list_drive_items(target.folder_id, marker)
            match = next((item for item in page.items if item.filename == name), None)
            if match or not page.next_marker:
                return match
            marker = page.next_marker

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        payload = await self._api_post(
            "/v2/file/copy",
            {
                "share_id": share.share_key,
                "file_id": source.remote_file_id,
                "to_drive_id": await self._get_drive_id(),
                "to_parent_file_id": target.folder_id,
                "auto_rename": False,
            },
            share_token=await self._get_share_token(share.share_key),
            retryable=False,
        )
        target_file_id = str(payload.get("file_id", ""))
        if not target_file_id:
            raise ProviderRequestError("Aliyun Drive copy returned no target file ID")
        return SaveResult(
            target_file_id=target_file_id,
            target_path=f"{target.path.rstrip('/')}/{source.filename}",
        )

    def consume_refresh_token_update(self) -> str | None:
        value = self._refresh_token_update
        self._refresh_token_update = None
        return value
