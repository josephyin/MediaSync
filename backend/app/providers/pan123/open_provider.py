from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

import httpx

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderNotConfiguredError,
    ProviderRequestError,
    ProviderWriteUncertainError,
)
from app.providers.base import (
    AccountProfile,
    DriveRef,
    FolderRef,
    RemoteItem,
    RemotePage,
    SaveResult,
    ShareInfo,
)

API_BASE_URL = "https://open-api.123pan.com"
DEFAULT_OPENLIST_TOKEN_URL = "https://api.oplist.org/123cloud/renewapi"
ROOT_FOLDER_ID = "0"


class Pan123OpenProvider:
    """123 Cloud Drive official account API adapter.

    OpenList mode exchanges a rotating refresh token through APIPages. Custom
    mode uses a user's own 123 open-platform client credentials directly.
    Share traversal and share-save deliberately stay on the private provider.
    """

    def __init__(
        self,
        *,
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        oauth_token_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.refresh_token = refresh_token.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.oauth_token_url = oauth_token_url.strip() if oauth_token_url else None
        self._http_client = http_client
        self._access_token: str | None = None
        self._refresh_token_update: str | None = None
        self.request_count = 0

    def _require_configuration(self) -> None:
        if self.oauth_token_url:
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
                    "123 OpenAPI token URL must be HTTPS without credentials or query"
                )
            if not self.refresh_token:
                raise ProviderNotConfiguredError(
                    "123 OpenAPI refresh token is required in OpenList mode"
                )
            return
        if not self.client_id or not self.client_secret:
            raise ProviderNotConfiguredError(
                "123 OpenAPI Client ID and Client Secret are required in custom mode"
            )

    @staticmethod
    def _payload(response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(f"123 OpenAPI {operation} returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError(f"123 OpenAPI {operation} returned invalid data")
        return payload

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
                        "driver_txt": "123cloud_oa",
                    },
                )
            else:
                response = await client.post(
                    f"{API_BASE_URL}/api/v1/access_token",
                    headers={"platform": "open_platform"},
                    json={"clientID": self.client_id, "clientSecret": self.client_secret},
                )
        except httpx.HTTPError as exc:
            raise ProviderRequestError("123 OpenAPI token request failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        payload = self._payload(response, "token request")
        if self.oauth_token_url:
            access_token = str(payload.get("access_token") or "")
            rotated = str(payload.get("refresh_token") or "")
            if response.is_error or not access_token or not rotated:
                raise ProviderRequestError("123 OpenList token refresh failed")
            if rotated != self.refresh_token:
                self.refresh_token = rotated
                self._refresh_token_update = rotated
        else:
            data = payload.get("data")
            access_token = str(data.get("accessToken") or "") if isinstance(data, dict) else ""
            if response.is_error or payload.get("code") != 0 or not access_token:
                raise ProviderRequestError("123 OpenAPI client credential request failed")
        self._access_token = access_token

    async def _request(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json: dict[str, object] | None = None,
        write: bool = False,
    ) -> dict[str, object]:
        await self._ensure_access_token()
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=20)
        try:
            self.request_count += 1
            response = await client.request(
                method,
                f"{API_BASE_URL}{path}",
                params=params,
                json=json,
                headers={
                    "authorization": f"Bearer {self._access_token}",
                    "platform": "open_platform",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            error_type = ProviderWriteUncertainError if write else ProviderRequestError
            raise error_type(f"123 OpenAPI {operation} request failed") from exc
        finally:
            if owns_client:
                await client.aclose()
        payload = self._payload(response, operation)
        if response.is_error or payload.get("code") != 0:
            message = str(payload.get("message") or "request rejected")
            error_type = (
                ProviderWriteUncertainError
                if write and response.status_code >= 500
                else ProviderRequestError
            )
            raise error_type(
                f"123 OpenAPI rejected the {operation} request "
                f"(http_status={response.status_code}, code={payload.get('code')}): {message}"
            )
        return payload

    async def validate_account(self) -> AccountProfile:
        payload = await self._request("account", "GET", "/api/v1/user/info")
        data = payload.get("data")
        uid = data.get("uid") if isinstance(data, dict) else None
        if isinstance(uid, bool) or not isinstance(uid, (int, str)) or not str(uid):
            raise ProviderRequestError("123 OpenAPI did not return a user ID")
        user_id = str(uid)
        return AccountProfile(
            identity=user_id,
            user_id=user_id,
            default_drive_id=ROOT_FOLDER_ID,
            drives=[DriveRef(ROOT_FOLDER_ID, "default", "默认盘")],
        )

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @classmethod
    def _item(cls, raw: dict[str, object]) -> RemoteItem:
        file_id = raw.get("fileId")
        name = raw.get("filename")
        item_type = raw.get("type")
        if isinstance(file_id, bool) or not isinstance(file_id, (int, str)):
            raise ProviderRequestError("123 OpenAPI item is missing its ID")
        if not isinstance(name, str) or not name or item_type not in {0, 1, 2}:
            raise ProviderRequestError("123 OpenAPI item has invalid metadata")
        parent_id = raw.get("parentFileId")
        size = raw.get("size")
        return RemoteItem(
            remote_file_id=str(file_id),
            parent_id=str(parent_id) if isinstance(parent_id, (int, str)) else None,
            filename=name,
            item_type="folder" if item_type == 1 else "file",
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            content_hash=str(raw.get("etag") or "") or None,
            updated_at=cls._parse_time(raw.get("updateAt")),
        )

    async def _list(self, parent_id: str, marker: str | None = None) -> RemotePage:
        if not parent_id.isdecimal():
            raise ValueError("123 OpenAPI folder ID must be numeric")
        if marker is not None and not marker.isdecimal():
            raise ValueError("123 OpenAPI page marker must be numeric")
        payload = await self._request(
            "directory listing",
            "GET",
            "/api/v2/file/list",
            params={
                "parentFileId": parent_id,
                "limit": 100,
                "lastFileId": marker or "0",
                "trashed": "false",
                "searchMode": "",
                "searchData": "",
            },
        )
        data = payload.get("data")
        items = data.get("fileList") if isinstance(data, dict) else None
        next_marker = data.get("lastFileId") if isinstance(data, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ProviderRequestError("123 OpenAPI directory listing returned invalid items")
        return RemotePage(
            items=[self._item(item) for item in items if item.get("trashed") in (None, 0)],
            next_marker=str(next_marker) if next_marker not in (None, -1, "-1") else None,
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        raise ProviderCapabilityError("123 OpenAPI does not expose share traversal")

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        raise ProviderCapabilityError("123 OpenAPI does not expose share traversal")

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        current = FolderRef(ROOT_FOLDER_ID, "/")
        for segment in (part for part in normalized.split("/") if part):
            item = await self.find_target_item(current, segment)
            if item is None or item.item_type != "folder":
                raise ProviderRequestError(f"123 OpenAPI folder does not exist: {normalized}")
            current = FolderRef(item.remote_file_id, f"{current.path.rstrip('/')}/{segment}")
        return current

    async def list_target_items(self, target: FolderRef, marker: str | None = None) -> RemotePage:
        return await self._list(target.folder_id, marker)

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        marker: str | None = None
        while True:
            page = await self.list_target_items(target, marker)
            match = next((item for item in page.items if item.filename == name), None)
            if match is not None or page.next_marker is None:
                return match
            marker = page.next_marker

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        if not name or "/" in name or name in {".", ".."}:
            raise ValueError("Invalid 123 folder name")
        existing = await self.find_target_item(parent, name)
        path = f"{parent.path.rstrip('/')}/{name}"
        if existing is not None:
            if existing.item_type != "folder":
                raise ProviderRequestError(f"123 OpenAPI target exists and is not a folder: {name}")
            return FolderRef(existing.remote_file_id, path)
        await self._request(
            "folder creation",
            "POST",
            "/upload/v1/file/mkdir",
            json={"parentID": parent.folder_id, "name": name},
            write=True,
        )
        created = await self.find_target_item(parent, name)
        if created is None or created.item_type != "folder":
            raise ProviderWriteUncertainError(
                "123 OpenAPI folder creation completed but the folder was not visible"
            )
        return FolderRef(created.remote_file_id, path)

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        raise ProviderCapabilityError("123 OpenAPI does not expose share saving")

    def consume_refresh_token_update(self) -> str | None:
        value = self._refresh_token_update
        self._refresh_token_update = None
        return value
