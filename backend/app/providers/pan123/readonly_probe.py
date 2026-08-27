from __future__ import annotations

import asyncio
import re
import secrets
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.exceptions import ProviderRequestError, ProviderWriteUncertainError

DRIVE_ORIGIN = "https://yun.123pan.com"
SHARE_ORIGIN = "https://www.123pan.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_ACCESS_TOKEN_LENGTH = 8_192
MAX_PAGE_SIZE = 100
SHARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,128}$")
SHARE_HOSTS = frozenset(
    {
        "123pan.com",
        "www.123pan.com",
        "123pan.cn",
        "www.123pan.cn",
        "123684.com",
        "www.123684.com",
        "123865.com",
        "www.123865.com",
    }
)
UID_SHARE_HOST_PATTERN = re.compile(
    r"^[1-9][0-9]{3,19}\.share\.(?:123pan\.cn|123865\.com|123684\.com)$"
)


class Pan123ProbeError(ProviderRequestError):
    code = "PAN123_PROBE_FAILED"


class Pan123AuthExpiredError(Pan123ProbeError):
    code = "PAN123_AUTH_EXPIRED"


class Pan123ShareInvalidError(Pan123ProbeError):
    code = "PAN123_SHARE_INVALID"


class Pan123RateLimitedError(Pan123ProbeError):
    code = "PAN123_RATE_LIMITED"


class Pan123RiskControlError(Pan123ProbeError):
    code = "PAN123_RISK_CONTROL"


class Pan123UpstreamChangedError(Pan123ProbeError):
    code = "PAN123_UPSTREAM_CHANGED"


class Pan123WriteRejectedError(Pan123ProbeError):
    code = "PAN123_WRITE_REJECTED"


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
    share: ListingProbeResult | None = None


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


def parse_share_url(share_url: str) -> tuple[str, str | None]:
    parsed = urlparse(share_url.strip())
    hostname = parsed.hostname or ""
    supported_host = hostname in SHARE_HOSTS or bool(
        UID_SHARE_HOST_PATTERN.fullmatch(hostname)
    )
    if parsed.scheme != "https" or not supported_host:
        raise ValueError("Share URL must use an official 123 Cloud Drive HTTPS domain")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("Share URL cannot contain credentials or a custom port")
    parts = [part for part in parsed.path.split("/") if part]
    valid_prefix = len(parts) == 2 and parts[0] in {"s", "123pan"}
    share_key = parts[1].removesuffix(".html") if valid_prefix else ""
    if not valid_prefix or not SHARE_KEY_PATTERN.fullmatch(share_key):
        raise ValueError("Invalid 123 Cloud Drive share URL")
    passwords = parse_qs(parsed.query).get("pwd", [])
    if len(passwords) > 1:
        raise ValueError("Share URL contains more than one password")
    password = passwords[0] if passwords else None
    if password and (len(password) > 32 or any(character.isspace() for character in password)):
        raise ValueError("Share password in URL is invalid")
    return share_key, password


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
        "Category",
        "CreateAt",
        "Etag",
        "FileId",
        "FileName",
        "ParentFileId",
        "Size",
        "Type",
        "UpdateAt",
    }
    return tuple(sorted(set().union(*(set(item) for item in items)) & allowed))


def build_request_signature(
    path: str,
    *,
    now: datetime | None = None,
    nonce: int | None = None,
) -> tuple[str, str]:
    """Build the query signature currently required by the 123 Web API."""
    if not path.startswith("/") or "//" in path:
        raise ValueError("123 Cloud Drive request path is invalid")
    current = (now or datetime.now(UTC)).astimezone(timezone_cst())
    timestamp = int(current.timestamp())
    random_value = secrets.randbelow(10_000_001) if nonce is None else nonce
    if not 0 <= random_value <= 10_000_000:
        raise ValueError("123 Cloud Drive signature nonce is out of range")
    table = "adefghlmyijnopkqrstubcvwsz"
    minute_code = "".join(table[int(character)] for character in current.strftime("%Y%m%d%H%M"))
    key = str(zlib.crc32(minute_code.encode()))
    unsigned = f"{timestamp}|{random_value}|{path}|web|3|{key}"
    value = f"{timestamp}-{random_value}-{zlib.crc32(unsigned.encode())}"
    return key, value


def timezone_cst() -> timezone:
    return timezone(timedelta(hours=8), name="UTC+8")


class Pan123ReadOnlyProbe:
    """Read-only, non-persistent probe for the 123 Web account/share APIs."""

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

    async def __aenter__(self) -> Pan123ReadOnlyProbe:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _headers(self, *, share: bool) -> dict[str, str]:
        origin = SHARE_ORIGIN if share else DRIVE_ORIGIN
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self._access_token}",
            "Origin": origin,
            "Referer": f"{origin}/",
            "Platform": "web",
            "App-Version": "3",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    async def _request(
        self,
        stage: str,
        origin: Literal["https://yun.123pan.com", "https://www.123pan.com"],
        path: str,
        *,
        method: Literal["GET", "POST"] = "GET",
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
        extra_headers: dict[str, str] | None = None,
        write_may_be_accepted: bool = False,
    ) -> dict[str, object]:
        if origin not in {DRIVE_ORIGIN, SHARE_ORIGIN}:
            raise Pan123UpstreamChangedError("123 probe refused an invalid fixed origin")
        try:
            signature_key, signature_value = build_request_signature(path)
        except ValueError as exc:
            raise Pan123UpstreamChangedError(str(exc)) from exc
        signed_params = dict(params or {})
        signed_params[signature_key] = signature_value
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)
            self._http_client = client
        response: httpx.Response | None = None
        max_retries = 0 if write_may_be_accepted else self._max_retries
        for attempt in range(max_retries + 1):
            try:
                self.request_count += 1
                headers = self._headers(share=origin == SHARE_ORIGIN)
                headers.update(extra_headers or {})
                response = await client.request(
                    method,
                    f"{origin}{path}",
                    params=signed_params,
                    json=body,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                if attempt < max_retries:
                    await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
                    continue
                label = "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
                if write_may_be_accepted:
                    raise ProviderWriteUncertainError(
                        f"123 Cloud Drive {stage} request {label}; do not submit again"
                    ) from exc
                raise Pan123ProbeError(f"123 Cloud Drive {stage} request {label}") from exc
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt < max_retries:
                await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
        if response is None:
            raise Pan123ProbeError(f"123 Cloud Drive {stage} request was not executed")
        if response.is_redirect:
            raise Pan123AuthExpiredError("123 Cloud Drive redirected the probe to login")
        try:
            payload: Any = response.json()
        except ValueError as exc:
            content_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
            if write_may_be_accepted:
                raise ProviderWriteUncertainError(
                    f"123 Cloud Drive {stage} returned an uncertain non-JSON response; "
                    "do not submit again"
                ) from exc
            raise Pan123UpstreamChangedError(
                f"123 Cloud Drive {stage} returned invalid JSON "
                f"(http_status={response.status_code}, content_type={content_type})"
            ) from exc
        if write_may_be_accepted and not isinstance(payload, dict):
            raise ProviderWriteUncertainError(
                f"123 Cloud Drive {stage} returned an uncertain response shape; "
                "do not submit again"
            )
        if not isinstance(payload, dict):
            raise Pan123UpstreamChangedError("123 Cloud Drive returned a non-object response")
        if write_may_be_accepted and response.status_code >= 500:
            raise ProviderWriteUncertainError(
                f"123 Cloud Drive {stage} returned an uncertain server error; "
                "do not submit again"
            )
        self._raise_for_error(stage, response.status_code, payload)
        return payload

    @staticmethod
    def _raise_for_error(stage: str, http_status: int, payload: dict[str, object]) -> None:
        code = _safe_int(payload.get("code"))
        if http_status < 400 and code == 0:
            return
        message = str(payload.get("message") or "").lower()
        if http_status == 429 or code == 429:
            raise Pan123RateLimitedError("123 Cloud Drive rate limited the read-only probe")
        if http_status in {401, 403} or code in {401, 403}:
            raise Pan123AuthExpiredError("123 Cloud Drive Access Token is expired or unauthorized")
        if any(marker in message for marker in ("captcha", "risk", "verify", "验证码", "风控")):
            raise Pan123RiskControlError("123 Cloud Drive requested additional verification")
        if stage == "share":
            raise Pan123ShareInvalidError(
                "123 Cloud Drive share is invalid, protected, or unavailable"
            )
        if stage == "share save":
            raise Pan123WriteRejectedError(
                "123 Cloud Drive rejected the share-save request "
                f"(http_status={http_status}, code={code})"
            )
        if any(marker in message for marker in ("login", "token", "未登录", "登录")):
            raise Pan123AuthExpiredError("123 Cloud Drive Access Token is expired or unauthorized")
        raise Pan123ProbeError(
            f"123 Cloud Drive rejected the {stage} request "
            f"(http_status={http_status}, code={code})"
        )

    @staticmethod
    def _data_object(payload: dict[str, object], stage: str) -> dict[str, object]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise Pan123UpstreamChangedError(
                f"123 Cloud Drive {stage} response contained no data object"
            )
        return data

    @classmethod
    def _listing_result(cls, payload: dict[str, object], stage: str) -> ListingProbeResult:
        data = cls._data_object(payload, stage)
        raw_items = data.get("InfoList")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise Pan123UpstreamChangedError(
                f"123 Cloud Drive {stage} response contained invalid items"
            )
        items: list[dict[str, object]] = raw_items
        return ListingProbeResult(
            item_count=len(items),
            total_count=_safe_int(data.get("Total")),
            field_names=_field_names(items),
        )

    async def probe_account(self) -> AccountProbeResult:
        payload = await self.fetch_account()
        data = self._data_object(payload, "account")
        if not any(field in data for field in ("UID", "Nickname", "SpaceUsed")):
            raise Pan123UpstreamChangedError(
                "123 Cloud Drive account response contained no stable account field"
            )
        return AccountProbeResult(session_accepted=True)

    async def fetch_account(self) -> dict[str, object]:
        return await self._request("account", DRIVE_ORIGIN, "/b/api/user/info")

    async def fetch_drive_page(
        self, parent_id: str, *, page_size: int = 10, page: int = 1
    ) -> dict[str, object]:
        return await self._request(
            "root" if parent_id == "0" else "folder",
            DRIVE_ORIGIN,
            "/b/api/file/list/new",
            params={
                "driveId": "0",
                "limit": str(page_size),
                "next": "0",
                "orderBy": "file_id",
                "orderDirection": "desc",
                "parentFileId": parent_id,
                "trashed": "false",
                "SearchData": "",
                "Page": str(page),
                "OnlyLookAbnormalFile": "0",
                "event": "homeListFile",
                "operateType": "4",
                "inDirectSpace": "false",
            },
        )

    async def probe_root(self, *, page_size: int = 10) -> ListingProbeResult:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        payload = await self.fetch_drive_page("0", page_size=page_size)
        return self._listing_result(payload, "root")

    async def fetch_share_page(
        self,
        share_url: str,
        *,
        share_password: str = "",
        parent_id: str = "0",
        page_size: int = 10,
        page: int = 1,
    ) -> tuple[str, str, dict[str, object]]:
        share_key, url_password = parse_share_url(share_url)
        password = share_password or url_password or ""
        payload = await self._request(
            "share",
            DRIVE_ORIGIN,
            "/b/api/share/get",
            params={
                "limit": str(page_size),
                "next": "0",
                "orderBy": "file_name",
                "orderDirection": "asc",
                "parentFileId": parent_id,
                "Page": str(page),
                "shareKey": share_key,
                "SharePwd": password,
            },
        )
        return share_key, password, payload

    async def probe_share(
        self,
        share_url: str,
        *,
        share_password: str = "",
        page_size: int = 10,
    ) -> ListingProbeResult:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        _share_key, _password, payload = await self.fetch_share_page(
            share_url,
            share_password=share_password,
            page_size=page_size,
        )
        return self._listing_result(payload, "share")

    async def run(
        self,
        *,
        share_url: str | None = None,
        share_password: str = "",
        page_size: int = 10,
        progress: Callable[[str], None] | None = None,
    ) -> ReadOnlyProbeReport:
        notify = progress or (lambda _stage: None)
        notify("account")
        account = await self.probe_account()
        notify("root")
        root = await self.probe_root(page_size=page_size)
        share = None
        if share_url is not None:
            notify("share")
            share = await self.probe_share(
                share_url,
                share_password=share_password,
                page_size=page_size,
            )
        notify("complete")
        return ReadOnlyProbeReport(account=account, root=root, share=share)
