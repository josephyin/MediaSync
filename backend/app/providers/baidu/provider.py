from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime

import httpx

from app.core.exceptions import ProviderCapabilityError, ProviderRequestError
from app.providers.baidu.open_provider import BaiduOpenProvider
from app.providers.baidu.share_probe import (
    BaiduShareReadOnlyProbe,
    BaiduShareUpstreamChangedError,
    _safe_int,
    parse_share_url,
)
from app.providers.baidu.write_client import BaiduWriteClient
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

ROOT_FOLDER_ID = "root"


class BaiduPrivateProvider:
    """Cookie-authenticated Baidu share traversal and transfer adapter."""

    def __init__(
        self,
        cookie: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        page_size: int = 100,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        probe = BaiduShareReadOnlyProbe(cookie, http_client=http_client)
        self._cookie = probe._cookie
        self._http_client = http_client
        self._page_size = page_size
        self._shares: dict[str, dict[str, object]] = {}
        self._item_paths: dict[tuple[str, str], str] = {}
        self._private_request_count = 0

    @property
    def request_count(self) -> int:
        return self._private_request_count

    def _probe(self, *, write: bool = False) -> BaiduShareReadOnlyProbe:
        cls = BaiduWriteClient if write else BaiduShareReadOnlyProbe
        return cls(self._cookie, http_client=self._http_client, max_retries=0)

    async def _finish(self, probe: BaiduShareReadOnlyProbe) -> None:
        self._private_request_count += probe.request_count
        await probe.aclose()

    @staticmethod
    def _page(marker: str | None) -> int:
        if marker is None:
            return 1
        if not marker.isdecimal() or not 1 <= int(marker) <= 1_000_000:
            raise ValueError("Invalid Baidu share page marker")
        return int(marker)

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
        if file_id is None or file_id <= 0 or not isinstance(name, str) or not name:
            raise BaiduShareUpstreamChangedError("Baidu share item is missing its ID or name")
        if isdir not in {0, 1}:
            raise BaiduShareUpstreamChangedError("Baidu share item has an invalid type")
        size = _safe_int(raw.get("size"))
        path = str(raw.get("path") or "")
        return RemoteItem(
            remote_file_id=str(file_id),
            parent_id=None,
            filename=name,
            item_type="folder" if isdir == 1 else "file",
            size=size if size is not None and size >= 0 else None,
            content_hash=str(raw.get("md5") or "") or None,
            updated_at=cls._parse_time(raw.get("server_mtime")),
            metadata={"path": path, "raw": raw},
        )

    async def validate_account(self) -> AccountProfile:
        probe = self._probe()
        try:
            data = await probe.fetch_account()
        finally:
            await self._finish(probe)
        user_id = str(data.get("user_id") or "")
        return AccountProfile(
            identity=str(data.get("user_name") or data.get("name") or user_id or "Baidu"),
            user_id=user_id or None,
            default_drive_id=ROOT_FOLDER_ID,
            drives=[DriveRef(ROOT_FOLDER_ID, "default", "默认盘")],
        )

    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo:
        share_key, _ = parse_share_url(share_url)
        probe = self._probe()
        try:
            _, effective_password, payload = await probe.fetch_share_page(
                share_url, password=password or "", page_size=self._page_size
            )
        finally:
            await self._finish(probe)
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise BaiduShareUpstreamChangedError("Baidu share returned invalid data")
        self._shares[share_key] = {
            "url": share_url,
            "password": effective_password,
            "data": data,
        }
        return ShareInfo(share_key=share_key, name="Baidu share", root_folder_id=ROOT_FOLDER_ID)

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ) -> RemotePage:
        context = self._shares.get(share.share_key)
        if context is None:
            raise ProviderRequestError("Resolve the Baidu share before listing its items")
        page = self._page(marker)
        probe = self._probe()
        try:
            if parent_id == ROOT_FOLDER_ID:
                _, _, payload = await probe.fetch_share_page(
                    str(context["url"]),
                    password=str(context["password"]),
                    page_size=self._page_size,
                    page=page,
                )
                data = payload.get("data")
            else:
                data0 = context["data"]
                assert isinstance(data0, dict)
                directory = self._item_paths.get((share.share_key, parent_id))
                if not directory:
                    raise ProviderRequestError("Baidu share folder path is unavailable")
                data = await probe.fetch_share_directory(
                    share_id=_safe_int(data0.get("shareid")) or 0,
                    source_uk=_safe_int(data0.get("uk")) or 0,
                    sekey=str(data0.get("seckey") or ""),
                    directory=directory,
                    page=page,
                    page_size=self._page_size,
                )
        finally:
            await self._finish(probe)
        if not isinstance(data, dict):
            raise BaiduShareUpstreamChangedError("Baidu share listing returned invalid data")
        raw_items = data.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise BaiduShareUpstreamChangedError("Baidu share listing returned invalid items")
        items = [self._item(item) for item in raw_items]
        for item in items:
            item.parent_id = parent_id
            path = item.metadata.get("path")
            if isinstance(path, str) and path:
                self._item_paths[(share.share_key, item.remote_file_id)] = path
        return RemotePage(items, str(page + 1) if len(items) == self._page_size else None)

    async def resolve_target_path(self, path: str) -> FolderRef:
        raise ProviderCapabilityError("Baidu target access requires an OpenAPI credential")

    async def list_target_items(self, target: FolderRef, marker: str | None = None) -> RemotePage:
        raise ProviderCapabilityError("Baidu target access requires an OpenAPI credential")

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        raise ProviderCapabilityError("Baidu folder creation requires an OpenAPI credential")

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        raise ProviderCapabilityError("Baidu target access requires an OpenAPI credential")

    async def save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> SaveResult:
        raise ProviderCapabilityError("Baidu share save requires the resumable write contract")

    async def submit_save(self, share: ShareInfo, source: RemoteItem, target: FolderRef) -> None:
        context = self._shares.get(share.share_key)
        if context is None:
            raise ProviderRequestError("Resolve the Baidu share before saving its items")
        try:
            source_id = int(source.remote_file_id)
        except ValueError as exc:
            raise ProviderRequestError("Baidu share item has an invalid file ID") from exc
        raw = {"fs_id": source_id}
        data = context.get("data")
        assert isinstance(data, dict)
        client = self._probe(write=True)
        assert isinstance(client, BaiduWriteClient)
        try:
            await client.save_share_item(
                share_url=str(context["url"]),
                share_data=data,
                source=raw,
                target_path=target.path,
            )
        finally:
            await self._finish(client)

    def consume_refresh_token_update(self) -> str | None:
        return None


class BaiduProvider(BaiduPrivateProvider):
    """Hybrid provider: Cookie for shares, OpenAPI for the account drive."""

    def __init__(
        self,
        cookie: str,
        open_provider: BaiduOpenProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        page_size: int = 100,
    ) -> None:
        super().__init__(cookie, http_client=http_client, page_size=page_size)
        self._open = open_provider

    async def resolve_target_path(self, path: str) -> FolderRef:
        return await self._open.resolve_target_path(path)

    async def list_target_items(self, target: FolderRef, marker: str | None = None) -> RemotePage:
        return await self._open.list_target_items(target, marker)

    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef:
        return await self._open.ensure_folder(parent, name)

    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None:
        return await self._open.find_target_item(target, name)

    @staticmethod
    def _fingerprint(item: RemoteItem) -> str:
        raw = f"{item.filename}\0{item.size or 0}\0{item.content_hash or ''}\0{item.item_type}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    @staticmethod
    def _operation_id(path: str, fingerprint: str) -> str:
        encoded = base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
        return f"baiduv1.{encoded}.{fingerprint}"

    @staticmethod
    def _parse_operation_id(operation_id: str) -> tuple[str, str]:
        parts = operation_id.split(".")
        if len(parts) != 3 or parts[0] != "baiduv1" or len(parts[2]) != 24:
            raise ValueError("Invalid Baidu operation ID")
        try:
            path = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode()
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid Baidu operation ID") from exc
        if not path.startswith("/") or not all(c in "0123456789abcdef" for c in parts[2]):
            raise ValueError("Invalid Baidu operation ID")
        return path, parts[2]

    async def start_save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> str:
        await self.submit_save(share, source, target)
        return self._operation_id(target.path, self._fingerprint(source))

    async def query_save_operation(self, operation_id: str) -> SaveOperation:
        target_path, fingerprint = self._parse_operation_id(operation_id)
        target = FolderRef(target_path, target_path)
        marker: str | None = None
        while True:
            page = await self._open.list_target_items(target, marker)
            match = next(
                (item for item in page.items if self._fingerprint(item) == fingerprint),
                None,
            )
            if match is not None:
                return SaveOperation(operation_id, True, (match.remote_file_id,))
            if page.next_marker is None:
                return SaveOperation(operation_id, False)
            marker = page.next_marker

    @property
    def request_count(self) -> int:
        return self._private_request_count + self._open.request_count

    def consume_open_refresh_token_update(self) -> str | None:
        return self._open.consume_refresh_token_update()
