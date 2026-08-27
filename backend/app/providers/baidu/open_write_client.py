from __future__ import annotations

from typing import Any

import httpx

from app.core.exceptions import ProviderWriteUncertainError
from app.providers.baidu.readonly_probe import (
    PAN_ORIGIN,
    BaiduAuthExpiredError,
    BaiduOpenReadOnlyProbe,
    BaiduProbeError,
    BaiduRateLimitedError,
    BaiduUpstreamChangedError,
    _safe_int,
)
from app.providers.baidu.write_client import normalize_target_path


class BaiduFolderWriteRejectedError(BaiduProbeError):
    code = "BAIDU_WRITE_REJECTED"


class BaiduOpenWriteClient(BaiduOpenReadOnlyProbe):
    """Official OpenAPI client for one non-replaying folder creation."""

    async def fetch_directory(
        self,
        path: str,
        *,
        page_size: int = 100,
    ) -> dict[str, object]:
        normalized = normalize_target_path(path)
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        payload = await self._request(
            "directory listing",
            "/rest/2.0/xpan/file",
            params={
                "method": "list",
                "dir": normalized,
                "start": 0,
                "limit": page_size,
                "web": "web",
                "order": "name",
            },
        )
        raw_items = payload.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise BaiduUpstreamChangedError("Baidu directory listing returned invalid items")
        return payload

    async def create_folder(self, path: str) -> None:
        normalized = normalize_target_path(path)
        if normalized == "/":
            raise ValueError("Baidu root folder cannot be created")
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)
            self._http_client = client
        try:
            self.request_count += 1
            response = await client.post(
                f"{PAN_ORIGIN}/rest/2.0/xpan/file",
                params={"method": "create", "access_token": self._access_token},
                data={"path": normalized, "size": "0", "isdir": "1", "rtype": "3"},
                headers={
                    "Accept": "application/json",
                    "User-Agent": "pan.baidu.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise ProviderWriteUncertainError(
                "Baidu folder creation request failed; do not submit again"
            ) from exc
        if response.status_code >= 500:
            raise ProviderWriteUncertainError(
                "Baidu folder creation returned an uncertain server error; do not submit again"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderWriteUncertainError(
                "Baidu folder creation returned an uncertain response; do not submit again"
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderWriteUncertainError(
                "Baidu folder creation returned an uncertain response; do not submit again"
            )
        errno = _safe_int(payload.get("errno"))
        if response.status_code in {401, 403} or errno in {-6, 110, 111}:
            raise BaiduAuthExpiredError("Baidu Netdisk Access Token is expired or invalid")
        if response.status_code == 429 or errno in {-7, 31034, 31045}:
            raise BaiduRateLimitedError("Baidu Netdisk rate limited the folder creation probe")
        if response.status_code >= 400 or errno not in (None, 0):
            raise BaiduFolderWriteRejectedError(
                "Baidu rejected the folder creation request "
                f"(http_status={response.status_code}, errno={errno})"
            )
