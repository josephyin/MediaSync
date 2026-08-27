from __future__ import annotations

import asyncio
import json
from pathlib import PurePosixPath
from typing import Any

import httpx

from app.core.exceptions import ProviderWriteUncertainError
from app.providers.baidu.share_probe import (
    PAN_ORIGIN,
    BaiduCookieExpiredError,
    BaiduShareProbeError,
    BaiduShareReadOnlyProbe,
    BaiduShareRiskControlError,
    BaiduShareUpstreamChangedError,
    _safe_int,
)


class BaiduWriteRejectedError(BaiduShareProbeError):
    code = "BAIDU_WRITE_REJECTED"


def normalize_target_path(value: str) -> str:
    path = value.strip()
    if not path.startswith("/") or len(path) > 1024:
        raise ValueError("Baidu target path must be an absolute path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError("Baidu target path contains control characters")
    normalized = str(PurePosixPath(path))
    if normalized != path.rstrip("/") and not (path == "/" and normalized == "/"):
        raise ValueError("Baidu target path must be normalized")
    if ".." in PurePosixPath(path).parts:
        raise ValueError("Baidu target path cannot contain parent traversal")
    return normalized


def decode_sekey(value: str) -> str:
    if not value or len(value) > 4096 or any(ord(character) < 32 for character in value):
        raise ValueError("Baidu share sekey is invalid")
    return value.replace("-", "+").replace("~", "=").replace("_", "/")


class BaiduWriteClient(BaiduShareReadOnlyProbe):
    """Minimal non-replaying client for one Baidu share-transfer request."""

    async def fetch_target_page(
        self,
        target_path: str,
        *,
        page_size: int = 100,
    ) -> dict[str, object]:
        path = normalize_target_path(target_path)
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)
            self._http_client = client
        try:
            self.request_count += 1
            response = await client.get(
                f"{PAN_ORIGIN}/api/list",
                params={
                    "dir": path,
                    "num": page_size,
                    "page": 1,
                    "order": "name",
                    "web": 1,
                },
                headers={
                    **self._headers(form=False),
                    "Referer": f"{PAN_ORIGIN}/disk/main",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise BaiduShareProbeError("Baidu target listing request failed") from exc
        if response.is_redirect or response.status_code in {401, 403}:
            raise BaiduCookieExpiredError("Baidu Cookie is expired or unauthorized")
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise BaiduShareUpstreamChangedError(
                "Baidu target listing returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise BaiduShareUpstreamChangedError(
                "Baidu target listing returned an unexpected response shape"
            )
        errno = _safe_int(payload.get("errno"))
        if response.status_code >= 400 or errno not in (None, 0):
            raise BaiduShareProbeError(
                "Baidu rejected the target listing request "
                f"(http_status={response.status_code}, errno={errno})"
            )
        raw_items = payload.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise BaiduShareUpstreamChangedError("Baidu target listing returned invalid items")
        return payload

    async def save_share_item(
        self,
        *,
        share_url: str,
        share_data: dict[str, object],
        source: dict[str, object],
        target_path: str,
    ) -> None:
        fs_id = _safe_int(source.get("fs_id"))
        if fs_id is None or fs_id <= 0:
            raise ValueError("Baidu share item has no valid fs_id")
        share_id = _safe_int(share_data.get("shareid"))
        source_uk = _safe_int(share_data.get("uk"))
        raw_sekey = share_data.get("seckey")
        if share_id is None or share_id <= 0:
            raise BaiduShareUpstreamChangedError("Baidu share contained no valid shareid")
        if source_uk is None or source_uk <= 0:
            raise BaiduShareUpstreamChangedError("Baidu share contained no valid source account")
        if not isinstance(raw_sekey, str):
            raise BaiduShareUpstreamChangedError("Baidu share contained no valid sekey")
        sekey = decode_sekey(raw_sekey)
        path = normalize_target_path(target_path)
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)
            self._http_client = client
        try:
            self.request_count += 1
            response = await client.post(
                f"{PAN_ORIGIN}/share/transfer",
                params={
                    "shareid": share_id,
                    "from": source_uk,
                    "sekey": sekey,
                    "bdstoken": "",
                    "channel": "chunlei",
                    "web": 1,
                    "app_id": 250528,
                    "clienttype": 0,
                },
                data={
                    "path": path,
                    "async": "2",
                    "fsidlist": json.dumps([fs_id], separators=(",", ":")),
                    "type": "0",
                    "channel": "chunlei",
                    "web": "1",
                    "app_id": "250528",
                    "clienttype": "0",
                },
                headers={
                    **self._headers(form=True),
                    "Referer": share_url,
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise ProviderWriteUncertainError(
                "Baidu share-transfer request failed; do not submit again"
            ) from exc
        if response.status_code >= 500:
            raise ProviderWriteUncertainError(
                "Baidu share-transfer returned an uncertain server error; do not submit again"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderWriteUncertainError(
                "Baidu share-transfer returned an uncertain response; do not submit again"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderWriteUncertainError(
                "Baidu share-transfer returned an uncertain response; do not submit again"
            )
        errno = _safe_int(payload.get("errno"))
        if response.status_code in {401, 403} or errno in {-6, 110, 111}:
            raise BaiduCookieExpiredError("Baidu Cookie is expired or unauthorized")
        message = str(payload.get("show_msg") or payload.get("errmsg") or "").lower()
        if any(marker in message for marker in ("captcha", "verify", "risk", "验证码", "风控")):
            raise BaiduShareRiskControlError("Baidu requested additional verification")
        if response.status_code >= 400 or errno not in (None, 0):
            raise BaiduWriteRejectedError(
                "Baidu rejected the share-transfer request "
                f"(http_status={response.status_code}, errno={errno})"
            )

    async def wait_for_target_item(
        self,
        target_path: str,
        *,
        source_name: str,
        source_size: int | None,
        poll_attempts: int,
        poll_interval: float,
    ) -> bool:
        for attempt in range(poll_attempts):
            payload = await self.fetch_target_page(target_path)
            items = payload["list"]
            assert isinstance(items, list)
            for item in items:
                assert isinstance(item, dict)
                if item.get("server_filename") != source_name:
                    continue
                target_size = _safe_int(item.get("size"))
                if source_size is None or target_size == source_size:
                    return True
            if attempt + 1 < poll_attempts:
                await asyncio.sleep(poll_interval)
        return False

