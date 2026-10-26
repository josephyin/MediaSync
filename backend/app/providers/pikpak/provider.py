from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderPaginationError,
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
from app.providers.pikpak.errors import PikPakCredentialError, PikPakRequestError

AUTH_URL = "https://user.mypikpak.com"
DRIVE_URL = "https://api-drive.mypikpak.com"
WEB_CLIENT_ID = "YUMx5nI8ZU8Ap8pm"
# Public Web 2.0.0 protocol constants (OpenList PikPak WebAlgorithms).
WEB_CAPTCHA_SALTS = (
    "C9qPpZLN8ucRTaTiUMWYS9cQvWOE",
    "+r6CQVxjzJV6LCV",
    "F",
    "pFJRC",
    "9WXYIDGrwTCz2OiVlgZa90qpECPD6olt",
    "/750aCr4lm/Sly/c",
    "RB+DT/gZCrbV",
    "",
    "CyLsf7hdkIRxRm215hl",
    "7xHvLi2tOYP0Y92b",
    "ZGTXXxu8E/MIWaEDB+Sm/",
    "1UI3",
    "E7fP5Pfijd+7K+t6Tg/NhuLq0eEUVChpJSkrKxpO",
    "ihtqpG6FMt65+Xk+tWUH2",
    "NhXXU9rg4XXdzo7u5o",
)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def parse_share_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"mypikpak.com", "www.mypikpak.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if len(parts) not in (2, 3) or parts[0] != "s":
            raise ValueError
        if not all(_ID.fullmatch(part) for part in parts[1:]):
            raise ValueError
        return parts[1], parts[2] if len(parts) == 3 else "root"
    except ValueError:
        raise PikPakRequestError("Use a PikPak HTTPS share URL: https://mypikpak.com/s/…") from None


class PikPakProvider:
    """Refresh-token login with single-submit, fingerprint-reconciled saves.

    Operation IDs contain only a target ID and a SHA-256 file fingerprint, never
    credentials or share passwords. A restored file must be complete and unique
    in the requested target before the operation is reported as successful.
    """

    def __init__(self, refresh_token: str, *, http_client: httpx.AsyncClient | None = None):
        value = refresh_token.strip()
        credentials = {"refresh_token": value}
        if value.startswith("{"):
            try:
                credentials = json.loads(value)
            except ValueError:
                raise PikPakRequestError("Invalid PikPak credential JSON") from None
        if not isinstance(credentials, dict):
            raise PikPakRequestError("Invalid PikPak credentials")
        token = credentials.get("refresh_token")
        if not isinstance(token, str) or not token.strip():
            raise PikPakRequestError("PikPak Refresh Token is required")
        self._credentials = {
            "refresh_token": token.strip(),
            "client_id": credentials.get("client_id", WEB_CLIENT_ID),
            "device_id": credentials.get("device_id", uuid4().hex),
        }
        if any(
            not isinstance(self._credentials[key], str) or not _ID.fullmatch(self._credentials[key])
            for key in ("client_id", "device_id")
        ):
            raise PikPakRequestError("Invalid PikPak client_id or device_id")
        self._http_client = http_client
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._captcha_token: str | None = None
        self._captcha_expires_at = 0.0
        self._token_update: str | None = None
        self._user_id: str | None = None
        self._share_tokens: dict[str, str] = {}
        self._share_passwords: dict[str, str] = {}
        self._share_root_pages: dict[str, dict] = {}
        self._listing_progress: dict[str, tuple[set[str], set[str]]] = {}
        self.request_count = 0

    async def _send(self, method: str, url: str, *, write: bool = False, **kwargs) -> dict:
        self.request_count += 1
        try:
            if self._http_client is not None:
                response = await self._http_client.request(
                    method, url, timeout=20, follow_redirects=False, **kwargs
                )
            else:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                    response = await client.request(method, url, **kwargs)
        except httpx.HTTPError:
            error = ProviderWriteUncertainError if write else PikPakRequestError
            raise error(
                "PikPak request interrupted; verify remote results before retrying writes"
            ) from None
        # Never include upstream bodies, URLs, or exception strings in logs/errors.
        error = ProviderWriteUncertainError if write else PikPakRequestError
        if response.status_code >= 500 or response.status_code in (408, 429):
            raise error(f"PikPak temporarily unavailable (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError:
            raise error("PikPak returned an invalid response") from None
        if not isinstance(payload, dict):
            raise error("PikPak returned an invalid response")
        if response.status_code in (401, 403) or payload.get("error_code") in (16, 4126):
            raise PikPakCredentialError(
                "PikPak authorization expired or requires verification; "
                "sign in on the official website and update the credential"
            )
        if not response.is_success or payload.get("error") or payload.get("error_code"):
            if 300 <= response.status_code < 400:
                raise error("PikPak returned an unexpected redirect")
            stages = {
                "/v1/auth/token": "刷新登录凭证",
                "/v1/shield/captcha/init": "申请验证令牌",
                "/v1/auth/signin": "账号登录",
                "/drive/v1/about": "校验账号",
                "/drive/v1/share": "读取分享信息",
                "/drive/v1/share/detail": "读取分享目录",
                "/drive/v1/files": "访问目标目录",
                "/drive/v1/share/restore": "转存分享文件",
            }
            reasons = {
                "captcha_invalid": "验证令牌无效",
                "captcha_required": "需要人机验证",
                "invalid_argument": "接口参数被拒绝",
                "invalid_request": "接口请求被拒绝",
                "invalid_account_or_password": "账号或密码错误",
                "file_restore_own": "不能转存当前账号自己的分享",
                "permission_denied": "没有访问权限",
                "file_not_found": "分享中的文件或子目录不存在，请重新复制目标目录的分享链接",
            }
            stage = stages.get(urlsplit(url).path, "请求")
            raw_error = payload.get("error")
            reason = (
                reasons.get(raw_error, "请求被拒绝") if isinstance(raw_error, str) else "请求被拒绝"
            )
            code = payload.get("error_code")
            suffix = f"，错误码 {code}" if type(code) is int else ""
            raise PikPakRequestError(
                f"PikPak {stage}失败：{reason}（HTTP {response.status_code}{suffix}）"
            )
        return payload

    async def _authenticate(self) -> None:
        if self._access_token and time.monotonic() < self._expires_at:
            return
        payload = await self._send(
            "POST",
            f"{AUTH_URL}/v1/auth/token",
            json={
                "client_id": self._credentials["client_id"],
                "refresh_token": self._credentials["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        token = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
            raise PikPakRequestError("PikPak token response is incomplete")
        self._access_token = token
        self._credentials["refresh_token"] = refresh
        self._token_update = json.dumps(self._credentials, separators=(",", ":"))
        self._user_id = payload.get("sub") if isinstance(payload.get("sub"), str) else None
        lifetime = payload.get("expires_in", 300)
        if isinstance(lifetime, str) and lifetime.isdecimal():
            lifetime = int(lifetime)
        if not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool):
            lifetime = 300
        self._expires_at = time.monotonic() + max(0, min(lifetime, 7200) - 30)

    async def _ensure_captcha(self, method: str, path: str) -> None:
        if self._credentials["client_id"] != WEB_CLIENT_ID:
            return
        if self._captcha_token and time.monotonic() < self._captcha_expires_at:
            return
        timestamp = str(int(time.time() * 1000))
        signature = WEB_CLIENT_ID + "2.0.0mypikpak.com" + self._credentials["device_id"] + timestamp
        for salt in WEB_CAPTCHA_SALTS:
            signature = hashlib.md5((signature + salt).encode()).hexdigest()
        payload = await self._send(
            "POST",
            f"{AUTH_URL}/v1/shield/captcha/init",
            json={
                "client_id": WEB_CLIENT_ID,
                "device_id": self._credentials["device_id"],
                "action": f"{method}:{path}",
                "meta": {
                    "client_version": "2.0.0",
                    "package_name": "mypikpak.com",
                    "user_id": self._user_id,
                    "timestamp": timestamp,
                    "captcha_sign": "1." + signature,
                },
            },
        )
        token = payload.get("captcha_token")
        if payload.get("url") or not isinstance(token, str) or not token:
            raise PikPakRequestError("PikPak 要求人机验证，请在官方网页完成验证后重试")
        self._captcha_token = token
        lifetime = payload.get("expires_in", 300)
        if isinstance(lifetime, str) and lifetime.isdecimal():
            lifetime = int(lifetime)
        if not isinstance(lifetime, (int, float)) or isinstance(lifetime, bool):
            lifetime = 300
        self._captcha_expires_at = time.monotonic() + max(0, min(lifetime, 300) - 30)

    async def _request(self, method: str, path: str, *, write: bool = False, **kwargs) -> dict:
        await self._authenticate()
        await self._ensure_captcha(method, path)
        return await self._send(
            method,
            f"{DRIVE_URL}{path}",
            write=write,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "X-Device-Id": self._credentials["device_id"],
                **({"X-Captcha-Token": self._captcha_token} if self._captcha_token else {}),
            },
            **kwargs,
        )

    async def validate_account(self) -> AccountProfile:
        payload = await self._request("GET", "/drive/v1/about")
        if not isinstance(payload.get("quota"), dict) or not self._user_id:
            raise PikPakRequestError("PikPak account response is incomplete")
        return AccountProfile(
            identity=f"PikPak {self._user_id}",
            user_id=self._user_id,
            default_drive_id="root",
            drives=[DriveRef("root", "default", "默认盘")],
        )

    @staticmethod
    def _parent(folder_id: str) -> str:
        if folder_id in ("", "root", "/"):
            return ""
        if not _ID.fullmatch(folder_id):
            raise PikPakRequestError("Invalid PikPak folder ID")
        return folder_id

    @staticmethod
    def _item(raw: object) -> RemoteItem:
        if not isinstance(raw, dict):
            raise PikPakRequestError("PikPak returned an invalid file")
        file_id, name, kind = raw.get("id"), raw.get("name"), raw.get("kind")
        if (
            not isinstance(file_id, str)
            or not _ID.fullmatch(file_id)
            or not isinstance(name, str)
            or not name
            or kind not in ("drive#file", "drive#folder")
        ):
            raise PikPakRequestError("PikPak file metadata is incomplete")
        size = raw.get("size")
        if isinstance(size, str) and size.isdecimal():
            size = int(size)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            size = None
        modified = raw.get("modified_time")
        try:
            updated_at = (
                datetime.fromisoformat(modified.replace("Z", "+00:00"))
                if isinstance(modified, str)
                else None
            )
        except ValueError:
            updated_at = None
        return RemoteItem(
            remote_file_id=file_id,
            parent_id=raw.get("parent_id") if isinstance(raw.get("parent_id"), str) else None,
            filename=name,
            item_type="folder" if kind == "drive#folder" else "file",
            size=size,
            updated_at=updated_at,
            content_hash=raw.get("hash") if isinstance(raw.get("hash"), str) else None,
            metadata={"phase": raw.get("phase")},
        )

    @classmethod
    def _page(cls, payload: dict, marker: str | None) -> RemotePage:
        files = payload.get("files")
        if not isinstance(files, list):
            raise PikPakRequestError("PikPak listing is missing files")
        next_marker = payload.get("next_page_token") or None
        if next_marker is not None and not isinstance(next_marker, str):
            raise PikPakRequestError("PikPak returned an invalid page token")
        if not files:
            return RemotePage([])
        if next_marker is not None and next_marker == marker:
            raise ProviderPaginationError("PikPak pagination did not advance")
        return RemotePage([cls._item(raw) for raw in files], next_marker)

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        share_id, parent = parse_share_url(share_url)
        payload = await self._request(
            "GET",
            "/drive/v1/share",
            params={
                "share_id": share_id,
                "pass_code": password or "",
                "limit": 100,
            },
        )
        if payload.get("share_status") != "OK":
            raise PikPakRequestError("PikPak share is unavailable; check its password or expiry")
        token = payload.get("pass_code_token", "")
        if not isinstance(token, str):
            raise PikPakRequestError("PikPak share authorization is invalid")
        self._share_tokens[share_id] = token
        self._share_passwords[share_id] = password or ""
        self._share_root_pages[share_id] = payload
        name = payload.get("title")
        return ShareInfo(share_id, name if isinstance(name, str) else "PikPak share", parent)

    def _share_token(self, share: ShareInfo) -> str:
        if share.share_key not in self._share_tokens:
            raise PikPakRequestError("Resolve the PikPak share before accessing its files")
        return self._share_tokens[share.share_key]

    async def list_share_items(
        self,
        share: ShareInfo,
        parent_id: str,
        marker: str | None = None,
    ) -> RemotePage:
        self._share_token(share)
        if parent_id in ("root", "", "/"):
            # /share/detail requires a real shared folder ID; an empty ID is
            # not the share root and returns file_not_found on live PikPak.
            if marker is None:
                return self._page(self._share_root_pages[share.share_key], None)
            payload = await self._request(
                "GET",
                "/drive/v1/share",
                params={
                    "share_id": share.share_key,
                    "pass_code": self._share_passwords[share.share_key],
                    "page_token": marker,
                    "limit": 100,
                },
            )
            if payload.get("share_status") != "OK":
                raise PikPakRequestError(
                    "PikPak share is unavailable; check its password or expiry"
                )
            return self._page(payload, marker)
        payload = await self._request(
            "GET",
            "/drive/v1/share/detail",
            params={
                "share_id": share.share_key,
                "pass_code_token": self._share_token(share),
                "parent_id": self._parent(parent_id),
                "page_token": marker or "",
                "limit": 100,
            },
        )
        return self._page(payload, marker)

    async def list_target_items(
        self,
        target: FolderRef,
        marker: str | None = None,
    ) -> RemotePage:
        payload = await self._request(
            "GET",
            "/drive/v1/files",
            params={
                "parent_id": self._parent(target.folder_id),
                "page_token": marker or "",
                "limit": 100,
                "filters": json.dumps({"trashed": {"eq": False}}),
            },
        )
        page = self._page(payload, marker)
        # The folder-picker API consumes pages directly, outside _items().
        # Keep a traversal guard here too so cyclic cursors cannot hang that API.
        key = self._parent(target.folder_id)
        if marker is None:
            self._listing_progress[key] = (set(), set())
        seen_markers, seen_ids = self._listing_progress.setdefault(key, (set(), set()))
        ids = {item.remote_file_id for item in page.items}
        if page.items and not ids.difference(seen_ids):
            raise ProviderPaginationError("PikPak listing repeated files without progress")
        if page.next_marker is not None and page.next_marker in seen_markers:
            raise ProviderPaginationError("PikPak listing repeated a page token")
        if page.next_marker is not None:
            seen_markers.add(page.next_marker)
        seen_ids.update(ids)
        return page

    async def _items(self, target: FolderRef) -> AsyncIterator[RemoteItem]:
        marker = None
        seen_markers: set[str] = set()
        seen_ids: set[str] = set()
        while True:
            page = await self.list_target_items(target, marker)
            new_items = [item for item in page.items if item.remote_file_id not in seen_ids]
            if page.items and not new_items:
                raise ProviderPaginationError("PikPak listing repeated files without progress")
            for item in new_items:
                seen_ids.add(item.remote_file_id)
                yield item
            if page.next_marker is None:
                return
            if page.next_marker in seen_markers:
                raise ProviderPaginationError("PikPak listing repeated a page token")
            seen_markers.add(page.next_marker)
            marker = page.next_marker

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        matches = [item async for item in self._items(target) if item.filename == name]
        if len(matches) > 1:
            raise PikPakRequestError("PikPak target contains ambiguous duplicate names")
        if matches and matches[0].metadata.get("phase") not in (None, "PHASE_TYPE_COMPLETE"):
            raise PikPakRequestError("PikPak target item is not complete yet")
        return matches[0] if matches else None

    def validate_existing_item(self, source: RemoteItem, existing: RemoteItem) -> None:
        if (
            not source.content_hash
            or source.size is None
            or self._fingerprint(source) != self._fingerprint(existing)
            or existing.metadata.get("phase") != "PHASE_TYPE_COMPLETE"
        ):
            raise PikPakRequestError(
                "PikPak target name exists but its complete fingerprint does not match; "
                "reconcile the target file before retrying"
            )

    async def resolve_target_path(self, path: str) -> FolderRef:
        current = FolderRef("root", "/")
        for segment in filter(None, path.split("/")):
            item = await self.find_target_item(current, segment)
            if item is None or item.item_type != "folder":
                raise PikPakRequestError("PikPak target folder does not exist")
            current = FolderRef(item.remote_file_id, f"{current.path.rstrip('/')}/{segment}")
        return current

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        if not name or name in (".", "..") or "/" in name or "\x00" in name:
            raise PikPakRequestError("Invalid PikPak folder name")
        existing = await self.find_target_item(parent, name)
        path = f"{parent.path.rstrip('/')}/{name}"
        if existing:
            if existing.item_type != "folder":
                raise PikPakRequestError("PikPak target name is occupied by a file")
            return FolderRef(existing.remote_file_id, path)
        payload = await self._request(
            "POST",
            "/drive/v1/files",
            write=True,
            json={
                "kind": "drive#folder",
                "name": name,
                "parent_id": self._parent(parent.folder_id),
            },
        )
        try:
            item = self._item(payload.get("file"))
        except PikPakRequestError:
            raise ProviderWriteUncertainError(
                "PikPak folder creation result is incomplete"
            ) from None
        if (
            item.item_type != "folder"
            or item.filename != name
            or self._parent(item.parent_id or "root") != self._parent(parent.folder_id)
        ):
            raise ProviderWriteUncertainError(
                "PikPak folder creation returned an unexpected target"
            )
        return FolderRef(item.remote_file_id, path)

    @staticmethod
    def _fingerprint(item: RemoteItem) -> str:
        value = json.dumps(
            [item.filename, item.size, item.content_hash, item.item_type],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(value.encode()).hexdigest()

    async def start_save_shared_item(
        self,
        share: ShareInfo,
        source: RemoteItem,
        target: FolderRef,
    ) -> str:
        token = self._share_token(share)
        # Scan jobs persist hash and size. Whole-folder restores cannot be verified
        # by their name alone; the scanner instead transfers individual files.
        if source.item_type != "file" or not source.content_hash or source.size is None:
            raise ProviderCapabilityError(
                "PikPak saves require an individual file with hash and size"
            )
        target_id = self._parent(target.folder_id) or "root"
        encoded = base64.urlsafe_b64encode(target_id.encode()).decode().rstrip("=")
        operation_id = f"pikpak1.{encoded}.{self._fingerprint(source)}"
        if len(operation_id) > 255:
            raise PikPakRequestError("PikPak target ID is too long")
        existing = await self.find_target_item(target, source.filename)
        if existing is not None:
            raise PikPakRequestError("PikPak target name already exists; reconcile before saving")
        payload = await self._request(
            "POST",
            "/drive/v1/share/restore",
            write=True,
            json={
                "share_id": share.share_key,
                "pass_code_token": token,
                "file_ids": [source.remote_file_id],
            },
        )
        if payload.get("restore_status") not in ("RESTORE_START", "RESTORE_COMPLETE"):
            raise ProviderWriteUncertainError("PikPak save acknowledgement is unrecognized")
        return operation_id

    async def query_save_operation(self, operation_id: str) -> SaveOperation:
        parts = operation_id.split(".")
        if len(parts) != 3 or parts[0] != "pikpak1" or not re.fullmatch(r"[0-9a-f]{64}", parts[2]):
            raise PikPakRequestError("Invalid PikPak operation ID")
        try:
            target_id = base64.b64decode(
                parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
            ).decode()
        except (ValueError, UnicodeError):
            raise PikPakRequestError("Invalid PikPak operation ID") from None
        self._parent(target_id)
        matches = [
            item
            async for item in self._items(FolderRef(target_id, "/"))
            if self._fingerprint(item) == parts[2]
        ]
        if not matches:
            # Share restore always lands in PikPak's default receiving folder;
            # it silently ignores the batch-copy API's `to` argument.
            roots = [item async for item in self._items(FolderRef("root", "/"))]
            receiving = [
                item
                for item in roots
                if item.item_type == "folder" and item.filename == "Pack From Shared"
            ]
            if len(receiving) > 1:
                raise ProviderWriteUncertainError("PikPak receiving folder is ambiguous")
            candidates = [
                item
                for folder in receiving
                async for item in self._items(FolderRef(folder.remote_file_id, "/"))
                if self._fingerprint(item) == parts[2]
            ]
            if len(candidates) > 1:
                raise ProviderWriteUncertainError("PikPak restored file is ambiguous")
            if candidates and candidates[0].metadata.get("phase") == "PHASE_TYPE_COMPLETE":
                await self._request(
                    "POST",
                    "/drive/v1/files:batchMove",
                    write=True,
                    json={
                        "ids": [candidates[0].remote_file_id],
                        "to": {"parent_id": self._parent(target_id)},
                    },
                )
                # An accepted move is not completion: verify destination again.
                matches = [
                    item
                    async for item in self._items(FolderRef(target_id, "/"))
                    if self._fingerprint(item) == parts[2]
                ]
        if any(
            self._parent(item.parent_id or "root") != self._parent(target_id) for item in matches
        ):
            raise ProviderWriteUncertainError("PikPak save result is outside the requested folder")
        if len(matches) > 1:
            raise ProviderWriteUncertainError(
                "PikPak save matched multiple files; reconcile manually"
            )
        if not matches or matches[0].metadata.get("phase") != "PHASE_TYPE_COMPLETE":
            return SaveOperation(operation_id, False)
        return SaveOperation(operation_id, True, (matches[0].remote_file_id,))

    async def save_shared_item(
        self,
        share: ShareInfo,
        source: RemoteItem,
        target: FolderRef,
    ) -> SaveResult:
        raise ProviderCapabilityError("PikPak share saving requires the resumable write contract")

    async def get_download_url(self, file_id: str) -> str:
        if not _ID.fullmatch(file_id):
            raise PikPakRequestError("Invalid PikPak file ID")
        payload = await self._request("GET", f"/drive/v1/files/{file_id}")
        url = payload.get("web_content_link")
        if not isinstance(url, str) or not url:
            raise PikPakRequestError("PikPak file has no download link")
        return url

    def consume_refresh_token_update(self) -> str | None:
        value, self._token_update = self._token_update, None
        return value
