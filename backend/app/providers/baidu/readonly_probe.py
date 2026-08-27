from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.exceptions import ProviderRequestError

PAN_ORIGIN = "https://pan.baidu.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ACCESS_TOKEN_LENGTH = 8_192
MAX_PAGE_SIZE = 100


class BaiduProbeError(ProviderRequestError):
    code = "BAIDU_PROBE_FAILED"


class BaiduAuthExpiredError(BaiduProbeError):
    code = "BAIDU_AUTH_EXPIRED"


class BaiduRateLimitedError(BaiduProbeError):
    code = "BAIDU_RATE_LIMITED"


class BaiduUpstreamChangedError(BaiduProbeError):
    code = "BAIDU_UPSTREAM_CHANGED"


@dataclass(frozen=True, slots=True)
class AccountProbeResult:
    session_accepted: bool


@dataclass(frozen=True, slots=True)
class ListingProbeResult:
    item_count: int
    total_count: int | None
    field_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadOnlyProbeReport:
    account: AccountProbeResult
    root: ListingProbeResult


def normalize_access_token(raw_token: str) -> str:
    if not isinstance(raw_token, str):
        raise ValueError("Access Token must be a string")
    value = raw_token.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value:
        raise ValueError("Access Token is required")
    if len(value) > MAX_ACCESS_TOKEN_LENGTH:
        raise ValueError("Access Token is too long")
    if len(value) < 16:
        raise ValueError("Access Token is too short")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError("Access Token contains whitespace or control characters")
    return value


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdecimal():
        return int(value)
    return None


def _field_names(items: list[dict[str, object]]) -> tuple[str, ...]:
    allowed = {
        "category",
        "fs_id",
        "isdir",
        "md5",
        "path",
        "server_ctime",
        "server_filename",
        "server_mtime",
        "size",
    }
    return tuple(sorted(set().union(*(set(item) for item in items)) & allowed))


class BaiduOpenReadOnlyProbe:
    """Read-only, non-persistent probe for Baidu Netdisk OpenAPI."""

    def __init__(
        self,
        access_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.5,
    ) -> None:
        if not 0 <= max_retries <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        if not 0 <= retry_backoff_seconds <= 5:
            raise ValueError("retry_backoff_seconds must be between 0 and 5")
        self._access_token = normalize_access_token(access_token)
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._owns_client = http_client is None
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self.request_count = 0

    async def __aenter__(self) -> BaiduOpenReadOnlyProbe:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _request(
        self,
        stage: str,
        path: str,
        *,
        params: dict[str, object],
    ) -> dict[str, object]:
        if path not in {"/rest/2.0/xpan/nas", "/rest/2.0/xpan/file"}:
            raise BaiduUpstreamChangedError("Baidu probe refused an invalid fixed path")
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)
            self._http_client = client
        request_params = dict(params)
        request_params["access_token"] = self._access_token
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                self.request_count += 1
                response = await client.get(
                    f"{PAN_ORIGIN}{path}",
                    params=request_params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "pan.baidu.com",
                    },
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
                    continue
                label = "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
                raise BaiduProbeError(f"Baidu Netdisk {stage} request {label}") from exc
            if response.status_code >= 500 and attempt < self._max_retries:
                await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
                continue
            break
        assert response is not None
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise BaiduUpstreamChangedError(
                f"Baidu Netdisk {stage} returned invalid JSON "
                f"(http_status={response.status_code}, content_type={content_type or 'unknown'})"
            ) from exc
        if not isinstance(payload, dict):
            raise BaiduUpstreamChangedError(
                f"Baidu Netdisk {stage} returned an unexpected response shape"
            )
        errno = _safe_int(payload.get("errno"))
        error_code = _safe_int(payload.get("error_code"))
        code = errno if errno not in (None, 0) else error_code
        if response.status_code in {401, 403} or code in {-6, 110, 111}:
            raise BaiduAuthExpiredError("Baidu Netdisk Access Token is expired or invalid")
        if response.status_code == 429 or code in {31034, 31045}:
            raise BaiduRateLimitedError("Baidu Netdisk rate limited the read-only probe")
        if response.status_code >= 400 or code not in (None, 0):
            raise BaiduProbeError(
                f"Baidu Netdisk rejected the {stage} request "
                f"(http_status={response.status_code}, errno={code})"
            )
        return payload

    async def probe_account(self) -> AccountProbeResult:
        payload = await self._request(
            "account",
            "/rest/2.0/xpan/nas",
            params={"method": "uinfo"},
        )
        if not any(key in payload for key in ("uk", "baidu_name", "netdisk_name", "vip_type")):
            raise BaiduUpstreamChangedError(
                "Baidu Netdisk account returned an unexpected response shape"
            )
        return AccountProbeResult(session_accepted=True)

    async def probe_root(self, *, page_size: int = 10) -> ListingProbeResult:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        payload = await self._request(
            "root listing",
            "/rest/2.0/xpan/file",
            params={
                "method": "list",
                "dir": "/",
                "start": 0,
                "limit": page_size,
                "web": "web",
            },
        )
        raw_items = payload.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise BaiduUpstreamChangedError(
                "Baidu Netdisk root listing returned an unexpected response shape"
            )
        items: list[dict[str, object]] = raw_items
        total_count = _safe_int(payload.get("total"))
        return ListingProbeResult(
            item_count=len(items),
            total_count=total_count,
            field_names=_field_names(items),
        )

    async def run(
        self,
        *,
        page_size: int = 10,
        progress: Callable[[str], None] | None = None,
    ) -> ReadOnlyProbeReport:
        if progress:
            progress("account")
        account = await self.probe_account()
        if progress:
            progress("root")
        root = await self.probe_root(page_size=page_size)
        if progress:
            progress("complete")
        return ReadOnlyProbeReport(account=account, root=root)

