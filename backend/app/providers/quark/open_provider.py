from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderNotConfiguredError,
    ProviderRequestError,
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

OPEN_API_ORIGIN = "https://open-api-drive.quark.cn"
ROOT_FOLDER_ID = "0"
DEFAULT_OPENLIST_TOKEN_URL = "https://api.oplist.org/quarkyun/renewapi"
MAX_MARKER_LENGTH = 2_048


class QuarkOpenProvider:
    """Quark OpenAPI adapter backed by an OpenList refresh-token broker.

    The OpenAPI covers the authenticated account's own drive. Share traversal
    and share-to-drive saving stay on the separately configured private
    provider and are deliberately unavailable here.
    """

    def __init__(
        self,
        refresh_token: str,
        app_id: str,
        sign_key: str,
        *,
        oauth_token_url: str = DEFAULT_OPENLIST_TOKEN_URL,
        api_base_url: str = OPEN_API_ORIGIN,
        http_client: httpx.AsyncClient | None = None,
        clock_ms: Callable[[], int] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.refresh_token = refresh_token.strip()
        self.app_id = app_id.strip()
        self.sign_key = sign_key.strip()
        self.oauth_token_url = oauth_token_url.strip()
        self.api_base_url = api_base_url.rstrip("/")
        self._http_client = http_client
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._request_id_factory = request_id_factory or (lambda: str(uuid.uuid4()))
        self._access_token: str | None = None
        self._refresh_token_update: str | None = None
        self.request_count = 0

    def _require_configuration(self) -> None:
        if not self.refresh_token:
            raise ProviderNotConfiguredError("Quark OpenAPI refresh token is required")
        if not self.app_id or not self.sign_key:
            raise ProviderNotConfiguredError(
                "Quark OpenAPI AppID and SignKey are required"
            )
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
                "Quark OpenAPI token URL must be an HTTPS URL without credentials or query"
            )
        api = urlparse(self.api_base_url)
        if api.scheme != "https" or api.hostname != "open-api-drive.quark.cn":
            raise ProviderNotConfiguredError("Quark OpenAPI origin is not approved")

    def _safe_message(self, value: object, fallback: str) -> str:
        message = str(value or fallback)
        for secret in (
            self.refresh_token,
            self.app_id,
            self.sign_key,
            self._access_token or "",
        ):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message

    def _message(self, payload: dict[str, object], fallback: str) -> str:
        return self._safe_message(
            payload.get("error_info")
            or payload.get("text")
            or payload.get("message")
            or fallback,
            fallback,
        )

    async def _ensure_access_token(self) -> None:
        if self._access_token:
            return
        self._require_configuration()
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=20)
        try:
            self.request_count += 1
            response = await client.get(
                self.oauth_token_url,
                params={
                    "refresh_ui": self.refresh_token,
                    "server_use": "true",
                    "driver_txt": "quarkyun_oa",
                },
            )
        except httpx.HTTPError as exc:
            # Do not include the exception text: the broker protocol places the
            # refresh token in the request URL.
            raise ProviderRequestError("Quark OpenList token refresh request failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                "Quark OpenList token refresh returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("Quark OpenList token refresh returned invalid data")
        if response.is_error or payload.get("text"):
            raise ProviderRequestError(
                f"Quark OpenList token refresh failed: "
                f"{self._message(payload, response.reason_phrase)}"
            )

        access_token = str(payload.get("access_token") or "")
        rotated_refresh_token = str(payload.get("refresh_token") or "")
        broker_app_id = str(payload.get("app_id") or "")
        broker_sign_key = str(payload.get("sign_key") or "")
        if broker_app_id:
            self.app_id = broker_app_id
        if broker_sign_key:
            self.sign_key = broker_sign_key
        if not access_token or not rotated_refresh_token:
            raise ProviderRequestError(
                "Quark OpenList token refresh did not return access and refresh tokens"
            )
        self._access_token = access_token
        if rotated_refresh_token != self.refresh_token:
            self.refresh_token = rotated_refresh_token
            self._refresh_token_update = rotated_refresh_token

    def _signed_headers(self, method: str, path: str) -> tuple[dict[str, str], str]:
        timestamp = str(self._clock_ms())
        digest = hashlib.sha256(
            f"{method}&{path}&{timestamp}&{self.sign_key}".encode()
        ).hexdigest()
        return (
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "User-Agent": "MediaSync/0.2 QuarkOpen",
                "x-pan-tm": timestamp,
                "x-pan-token": digest,
                "x-pan-client-id": self.app_id,
            },
            self._request_id_factory(),
        )

    async def _request(
        self,
        path: str,
        method: str,
        body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not path.startswith("/open/v1/") or "//" in path:
            raise ValueError("Invalid Quark OpenAPI path")
        await self._ensure_access_token()
        headers, request_id = self._signed_headers(method, path)
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=20)
        try:
            self.request_count += 1
            response = await client.request(
                method,
                f"{self.api_base_url}{path}",
                params={"req_id": request_id, "access_token": self._access_token},
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            # Access tokens are query parameters in the upstream protocol.
            raise ProviderRequestError("Quark OpenAPI request failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Quark OpenAPI returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("Quark OpenAPI returned invalid data")
        raw_status = payload.get("status", 0)
        raw_errno = payload.get("errno", 0)
        if response.is_error or raw_status not in (0, 200, None) or raw_errno not in (0, None):
            code = raw_errno or raw_status or response.status_code
            raise ProviderRequestError(
                f"Quark OpenAPI {code}: {self._message(payload, response.reason_phrase)}"
            )
        return payload

    @staticmethod
    def _data(payload: dict[str, object], operation: str) -> dict[str, object]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderRequestError(f"Quark OpenAPI {operation} returned no data object")
        return data

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
        file_id = str(raw.get("fid") or "")
        filename = str(raw.get("filename") or "")
        if not file_id or not filename:
            raise ProviderRequestError("Quark OpenAPI file item is missing its ID or name")
        size = raw.get("size")
        return RemoteItem(
            remote_file_id=file_id,
            parent_id=str(raw.get("parent_fid") or "") or None,
            filename=filename,
            item_type="folder" if str(raw.get("file_type")) == "0" else "file",
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
            content_hash=str(raw.get("content_hash") or "") or None,
            updated_at=cls._parse_time(raw.get("updated_at")),
        )

    @staticmethod
    def _encode_marker(cursor: dict[str, object]) -> str:
        raw = json.dumps(cursor, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_marker(marker: str | None) -> dict[str, object] | None:
        if marker is None:
            return None
        if not marker or len(marker) > MAX_MARKER_LENGTH:
            raise ValueError("Invalid Quark OpenAPI page marker")
        try:
            padded = marker + "=" * (-len(marker) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid Quark OpenAPI page marker") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("token"), str):
            raise ValueError("Invalid Quark OpenAPI page marker")
        version = decoded.get("version")
        if version is not None and not isinstance(version, str):
            raise ValueError("Invalid Quark OpenAPI page marker")
        return {"token": decoded["token"], "version": version or ""}

    async def validate_account(self) -> AccountProfile:
        payload = await self._request("/open/v1/user/info", "GET")
        data = self._data(payload, "user info")
        user_id = str(data.get("user_id") or "")
        if not user_id:
            raise ProviderRequestError("Quark OpenAPI did not return a user ID")
        return AccountProfile(
            identity=str(data.get("nickname") or user_id),
            user_id=user_id,
            default_drive_id=ROOT_FOLDER_ID,
            drives=[DriveRef(ROOT_FOLDER_ID, "default", "默认盘")],
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        raise ProviderCapabilityError(
            "Quark OpenAPI does not expose share traversal; use the private provider"
        )

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        raise ProviderCapabilityError(
            "Quark OpenAPI does not expose share traversal; use the private provider"
        )

    async def _list_drive_items(self, parent_id: str, marker: str | None = None) -> RemotePage:
        body: dict[str, object] = {
            "parent_fid": parent_id,
            "size": 100,
            "sort": "file_name:asc",
        }
        cursor = self._decode_marker(marker)
        if cursor is not None:
            body["query_cursor"] = cursor
        payload = await self._request("/open/v1/file/list", "POST", body)
        data = self._data(payload, "file list")
        raw_items = data.get("file_list")
        if not isinstance(raw_items, list) or any(
            not isinstance(item, dict) for item in raw_items
        ):
            raise ProviderRequestError("Quark OpenAPI file list returned invalid items")
        next_marker = None
        if data.get("last_page") is not True:
            raw_cursor = data.get("next_query_cursor")
            if not isinstance(raw_cursor, dict) or not raw_cursor.get("token"):
                raise ProviderRequestError("Quark OpenAPI file list returned no next cursor")
            next_marker = self._encode_marker(raw_cursor)
        return RemotePage(
            items=[self._to_remote_item(item) for item in raw_items],
            next_marker=next_marker,
        )

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        current = FolderRef(ROOT_FOLDER_ID, "/")
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
                raise ProviderRequestError(f"Quark OpenAPI folder does not exist: {normalized}")
            current = FolderRef(match.remote_file_id, f"{current.path.rstrip('/')}/{segment}")
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
            if existing is not None:
                return FolderRef(existing.remote_file_id, f"{parent.path.rstrip('/')}/{name}")
            if page.next_marker is None:
                break
            marker = page.next_marker
        payload = await self._request(
            "/open/v1/dir",
            "POST",
            {"dir_path": name, "pdir_fid": parent.folder_id},
        )
        data = self._data(payload, "folder creation")
        folder_id = str(data.get("fid") or "")
        if not folder_id:
            raise ProviderRequestError("Quark OpenAPI folder creation returned no file ID")
        return FolderRef(folder_id, f"{parent.path.rstrip('/')}/{name}")

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        marker: str | None = None
        while True:
            page = await self._list_drive_items(target.folder_id, marker)
            match = next((item for item in page.items if item.filename == name), None)
            if match is not None or page.next_marker is None:
                return match
            marker = page.next_marker

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        raise ProviderCapabilityError(
            "Quark OpenAPI does not expose share saving; use the private provider"
        )

    def consume_refresh_token_update(self) -> str | None:
        value = self._refresh_token_update
        self._refresh_token_update = None
        return value
