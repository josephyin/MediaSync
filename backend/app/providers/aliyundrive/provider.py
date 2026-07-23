from datetime import datetime
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


class AliyunDriveProvider:
    """Aliyun Drive official OpenAPI adapter.

    The official OpenAPI is used for OAuth and the authenticated user's drive.
    Share traversal/saving remains disabled until that capability is explicitly
    granted and documented for the MediaSync developer application.
    """

    def __init__(
        self,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        api_base_url: str = "https://openapi.alipan.com",
        oauth_token_url: str | None = None,
        drive_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base_url = api_base_url.rstrip("/")
        self.oauth_token_url = oauth_token_url.strip() if oauth_token_url else None
        self._http_client = http_client
        self._access_token: str | None = None
        self._refresh_token_update: str | None = None
        self._drive_id: str | None = drive_id
        self._selected_drive_id = drive_id
        self._drives: list[DriveRef] = []

    def _require_oauth_config(self) -> None:
        if not self.oauth_token_url and (not self.client_id or not self.client_secret):
            raise ProviderNotConfiguredError(
                "Aliyun Drive OAuth is not configured. Set ALIYUNDRIVE_CLIENT_ID "
                "and ALIYUNDRIVE_CLIENT_SECRET from an approved developer application."
            )

    async def _post(
        self, path: str, body: dict[str, object], *, authenticated: bool = True
    ) -> dict[str, object]:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            await self._ensure_access_token()
            headers["Authorization"] = f"Bearer {self._access_token}"
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=20)
        try:
            response = await client.post(f"{self.api_base_url}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderRequestError(f"Aliyun Drive request failed: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Aliyun Drive returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if response.is_error or payload.get("code"):
            code = payload.get("code", response.status_code)
            message = payload.get("message", response.reason_phrase)
            raise ProviderRequestError(f"Aliyun Drive {code}: {message}")
        return payload

    async def _ensure_access_token(self) -> None:
        if self._access_token:
            return
        self._require_oauth_config()
        if self.oauth_token_url:
            owns_client = self._http_client is None
            client = self._http_client or httpx.AsyncClient(timeout=20)
            try:
                response = await client.get(
                    self.oauth_token_url,
                    params={
                        "refresh_ui": self.refresh_token,
                        "server_use": "true",
                        "driver_txt": "alicloud_qr",
                    },
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise ProviderRequestError(
                        "Aliyun Drive hosted OAuth returned invalid JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ProviderRequestError("Aliyun Drive hosted OAuth returned invalid data")

                has_tokens = payload.get("access_token") and payload.get("refresh_token")
                message = str(payload.get("text") or payload.get("message") or "")
                should_use_legacy_post = not has_tokens and (
                    bool(payload.get("code")) or "Incorrect GrantType" in message
                )
                if should_use_legacy_post:
                    response = await client.post(
                        self.oauth_token_url,
                        json={
                            "client_id": self.client_id,
                            "client_secret": self.client_secret,
                            "grant_type": "refresh_token",
                            "refresh_token": self.refresh_token,
                        },
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ProviderRequestError(
                            "Aliyun Drive hosted OAuth legacy endpoint returned invalid JSON"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ProviderRequestError(
                            "Aliyun Drive hosted OAuth legacy endpoint returned invalid data"
                        )
            except httpx.HTTPError as exc:
                # The GET protocol sends the refresh token as a query parameter.
                # Do not include httpx's exception text because it may contain
                # the complete request URL.
                raise ProviderRequestError("Aliyun Drive hosted OAuth request failed") from exc
            finally:
                if owns_client:
                    await client.aclose()
            if response.is_error or (
                payload.get("text")
                and not (payload.get("access_token") and payload.get("refresh_token"))
            ):
                message = payload.get("text") or payload.get("message") or response.reason_phrase
                raise ProviderRequestError(f"Aliyun Drive hosted OAuth failed: {message}")
        else:
            payload = await self._post(
                "/oauth/access_token",
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
                authenticated=False,
            )
        access_token = str(payload.get("access_token", ""))
        rotated_refresh_token = str(payload.get("refresh_token", ""))
        if not access_token or not rotated_refresh_token:
            raise ProviderRequestError("Aliyun Drive OAuth response did not contain both tokens")
        self._access_token = access_token
        if rotated_refresh_token != self.refresh_token:
            self.refresh_token = rotated_refresh_token
            self._refresh_token_update = rotated_refresh_token

    async def _drive_info(self) -> dict[str, object]:
        payload = await self._post("/adrive/v1.0/user/getDriveInfo", {})
        labels = {"default": "默认盘", "resource": "资源库", "backup": "备份盘"}
        drives: list[DriveRef] = []
        seen: set[str] = set()
        for drive_type in ("default", "resource", "backup"):
            drive_id = str(payload.get(f"{drive_type}_drive_id") or "")
            if drive_id and drive_id not in seen:
                drives.append(DriveRef(id=drive_id, type=drive_type, name=labels[drive_type]))
                seen.add(drive_id)
        self._drives = drives
        if not self._selected_drive_id and drives:
            self._drive_id = next(
                (item.id for item in drives if item.type == "resource"), drives[0].id
            )
        if not self._drive_id:
            raise ProviderRequestError("Aliyun Drive did not return an available drive ID")
        return payload

    async def _get_drive_id(self) -> str:
        if not self._drive_id:
            await self._drive_info()
        return str(self._drive_id)

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
        return RemoteItem(
            remote_file_id=str(raw.get("file_id", "")),
            parent_id=str(raw.get("parent_file_id", "")) or None,
            filename=str(raw.get("name") or raw.get("file_name") or ""),
            item_type=str(raw.get("type", "file")),
            size=int(raw["size"]) if raw.get("size") is not None else None,
            content_hash=str(raw.get("content_hash", "")) or None,
            updated_at=cls._parse_time(raw.get("updated_at")),
            metadata={"drive_id": raw.get("drive_id")},
        )

    async def validate_account(self) -> AccountProfile:
        payload = await self._drive_info()
        identity = str(payload.get("user_name") or payload.get("user_id") or "Aliyun Drive")
        return AccountProfile(
            identity=identity,
            user_id=str(payload.get("user_id") or "") or None,
            default_drive_id=self._drive_id,
            drives=self._drives,
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        parsed = urlparse(share_url)
        allowed_hosts = {"www.alipan.com", "alipan.com", "www.aliyundrive.com", "aliyundrive.com"}
        if parsed.hostname not in allowed_hosts:
            raise ValueError("Share URL must use an Aliyun Drive domain")
        parts = [part for part in parsed.path.split("/") if part]
        share_prefix = next(
            (index for index, part in enumerate(parts) if part in {"s", "share"}), -1
        )
        if share_prefix < 0 or share_prefix + 1 >= len(parts):
            raise ValueError("Invalid Aliyun Drive share URL")
        share_key = parts[share_prefix + 1]
        return ShareInfo(share_key=share_key, name=f"Aliyun share {share_key}")

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        raise ProviderCapabilityError(
            "The approved Aliyun Drive OpenAPI contract does not currently expose "
            "share traversal to MediaSync."
        )

    async def _list_drive_items(self, parent_id: str, marker: str | None = None) -> RemotePage:
        payload = await self._post(
            "/adrive/v1.0/openFile/list",
            {
                "drive_id": await self._get_drive_id(),
                "parent_file_id": parent_id,
                "limit": 200,
                "marker": marker or "",
                "order_by": "name",
                "order_direction": "ASC",
            },
        )
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise ProviderRequestError("Aliyun Drive file list returned an invalid items field")
        return RemotePage(
            items=[self._to_remote_item(item) for item in raw_items if isinstance(item, dict)],
            next_marker=str(payload.get("next_marker", "")) or None,
        )

    async def resolve_target_path(self, path: str) -> FolderRef:
        normalized = "/" + path.strip("/") if path.strip("/") else "/"
        current = FolderRef(folder_id="root", path="/")
        if normalized == "/":
            await self._get_drive_id()
            return current
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
                path=str(current.path.rstrip("/") + "/" + part),
            )
        return current

    async def list_target_items(self, target: FolderRef, marker: str | None = None) -> RemotePage:
        return await self._list_drive_items(target.folder_id, marker)

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        page = await self._list_drive_items(parent.folder_id)
        existing = next(
            (item for item in page.items if item.item_type == "folder" and item.filename == name),
            None,
        )
        if existing:
            return FolderRef(existing.remote_file_id, f"{parent.path.rstrip('/')}/{name}")
        payload = await self._post(
            "/adrive/v1.0/openFile/create",
            {
                "drive_id": await self._get_drive_id(),
                "parent_file_id": parent.folder_id,
                "name": name,
                "type": "folder",
                "check_name_mode": "refuse",
            },
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
        raise ProviderCapabilityError(
            "Share-to-drive saving is not available in the approved Aliyun Drive "
            "OpenAPI contract configured for MediaSync."
        )

    def consume_refresh_token_update(self) -> str | None:
        value = self._refresh_token_update
        self._refresh_token_update = None
        return value
