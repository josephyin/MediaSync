from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.exceptions import ProviderRequestError, ProviderWriteUncertainError

PAN_ORIGIN = "https://pan.quark.cn"
DRIVE_ORIGIN = "https://drive.quark.cn"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_COOKIE_LENGTH = 16_384
MAX_PAGE_SIZE = 50
ROTATABLE_COOKIE_NAMES = frozenset({"__puus", "__pus"})
COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
SHARE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,128}$")


class QuarkProbeError(ProviderRequestError):
    code = "QUARK_PROBE_FAILED"


class QuarkAuthExpiredError(QuarkProbeError):
    code = "QUARK_AUTH_EXPIRED"


class QuarkShareInvalidError(QuarkProbeError):
    code = "QUARK_SHARE_INVALID"


class QuarkRateLimitedError(QuarkProbeError):
    code = "QUARK_RATE_LIMITED"


class QuarkRiskControlError(QuarkProbeError):
    code = "QUARK_RISK_CONTROL"


class QuarkUpstreamChangedError(QuarkProbeError):
    code = "QUARK_UPSTREAM_CHANGED"


class QuarkWriteRejectedError(QuarkProbeError):
    code = "QUARK_WRITE_REJECTED"


@dataclass(frozen=True, slots=True)
class AccountProbeResult:
    session_accepted: bool


@dataclass(frozen=True, slots=True)
class ListingProbeResult:
    item_count: int
    total_count: int | None
    field_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShareProbeResult:
    item_count: int
    total_count: int | None
    field_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadOnlyProbeReport:
    account: AccountProbeResult
    root: ListingProbeResult
    share: ShareProbeResult | None = None
    rotated_cookie_names: tuple[str, ...] = field(default_factory=tuple)


def normalize_cookie(raw_cookie: str) -> str:
    if not isinstance(raw_cookie, str):
        raise ValueError("Cookie must be a string")
    value = raw_cookie.strip()
    if not value:
        raise ValueError("Cookie is required")
    if len(value) > MAX_COOKIE_LENGTH:
        raise ValueError("Cookie is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Cookie contains control characters")

    parsed: dict[str, str] = {}
    for part in value.split(";"):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError("Cookie contains an invalid item")
        name, cookie_value = item.split("=", 1)
        name = name.strip()
        cookie_value = cookie_value.strip()
        if not COOKIE_NAME_PATTERN.fullmatch(name):
            raise ValueError("Cookie contains an invalid name")
        if name in parsed:
            raise ValueError("Cookie contains a duplicate name")
        if any(ord(character) < 32 or ord(character) == 127 for character in cookie_value):
            raise ValueError("Cookie contains an invalid value")
        parsed[name] = cookie_value
    if not parsed:
        raise ValueError("Cookie contains no key-value items")
    return "; ".join(f"{name}={cookie_value}" for name, cookie_value in parsed.items())


def parse_share_url(share_url: str) -> str:
    parsed = urlparse(share_url.strip())
    if parsed.scheme != "https" or parsed.hostname != "pan.quark.cn":
        raise ValueError("Share URL must use https://pan.quark.cn")
    if parse_qs(parsed.query).get("pwd"):
        raise ValueError("Remove the share password from the URL and enter it at the prompt")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "s" or not SHARE_ID_PATTERN.fullmatch(parts[1]):
        raise ValueError("Invalid Quark Drive share URL")
    return parts[1]


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _field_names(items: list[dict[str, object]]) -> tuple[str, ...]:
    allowed = {
        "category",
        "created_at",
        "dir",
        "fid",
        "file",
        "file_name",
        "file_type",
        "obj_category",
        "pdir_fid",
        "share_fid_token",
        "size",
        "updated_at",
    }
    return tuple(sorted(set().union(*(set(item) for item in items)) & allowed))


class QuarkReadOnlyProbe:
    """Read-only, non-persistent probe for experimental Quark Web APIs."""

    def __init__(
        self,
        cookie: str,
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
        self._cookie = normalize_cookie(cookie)
        self._http_client = http_client
        self._timeout_seconds = timeout_seconds
        self._owns_client = http_client is None
        self._rotated_cookie_names: set[str] = set()
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self.request_count = 0

    async def __aenter__(self) -> QuarkReadOnlyProbe:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    @property
    def rotated_cookie_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._rotated_cookie_names))

    @property
    def current_cookie(self) -> str:
        """Return the in-memory Cookie for another Quark component in this process."""
        return self._cookie

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": self._cookie,
            "Origin": PAN_ORIGIN,
            "Referer": f"{PAN_ORIGIN}/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    async def _request(
        self,
        stage: str,
        method: Literal["GET", "POST"],
        origin: Literal["https://pan.quark.cn", "https://drive.quark.cn"],
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
        write_may_be_accepted: bool = False,
    ) -> dict[str, object]:
        if origin not in {PAN_ORIGIN, DRIVE_ORIGIN}:
            raise QuarkUpstreamChangedError("Quark probe refused an invalid fixed origin")
        if not path.startswith("/") or "//" in path:
            raise QuarkUpstreamChangedError("Quark probe refused an invalid fixed path")
        client = self._http_client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds, follow_redirects=False)
            self._http_client = client
        response: httpx.Response | None = None
        for attempt in range(self._max_retries + 1):
            try:
                self.request_count += 1
                response = await client.request(
                    method,
                    f"{origin}{path}",
                    params=params,
                    json=body,
                    headers=self._headers(),
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
                    continue
                label = "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
                error_type = (
                    ProviderWriteUncertainError
                    if write_may_be_accepted
                    else QuarkProbeError
                )
                raise error_type(f"Quark {stage} request {label}") from exc
            self._merge_rotated_cookies(response)
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt < self._max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else None
                except ValueError:
                    delay = None
                if delay is None or not 0 <= delay <= 5:
                    delay = self._retry_backoff_seconds * (2**attempt)
                await asyncio.sleep(delay)
        if response is None:
            raise QuarkProbeError(f"Quark {stage} read-only request was not executed")

        if response.is_redirect:
            raise QuarkAuthExpiredError("Quark redirected the read-only probe to login")
        if response.status_code == 429:
            raise QuarkRateLimitedError("Quark rate limited the read-only probe")
        if response.status_code in {401, 403}:
            raise QuarkAuthExpiredError("Quark Cookie is expired or unauthorized")
        if response.status_code >= 500:
            if write_may_be_accepted:
                raise ProviderWriteUncertainError(
                    f"Quark {stage} returned an uncertain server error"
                )
            raise QuarkProbeError("Quark read-only upstream is temporarily unavailable")
        try:
            payload = self._json_object(response)
        except QuarkUpstreamChangedError as exc:
            if write_may_be_accepted:
                raise ProviderWriteUncertainError(
                    f"Quark {stage} returned an uncertain response"
                ) from exc
            raise
        self._raise_for_error(stage, response.status_code, payload)
        return payload

    def _merge_rotated_cookies(self, response: httpx.Response) -> None:
        updates: dict[str, str] = {}
        for header in response.headers.get_list("set-cookie"):
            parsed = SimpleCookie()
            try:
                parsed.load(header)
            except Exception:
                continue
            for name, morsel in parsed.items():
                if name in ROTATABLE_COOKIE_NAMES and morsel.value:
                    updates[name] = morsel.value
        if not updates:
            return
        current = {
            item.split("=", 1)[0].strip(): item.split("=", 1)[1].strip()
            for item in self._cookie.split(";")
        }
        current.update(updates)
        self._cookie = normalize_cookie(
            "; ".join(f"{name}={value}" for name, value in current.items())
        )
        self._rotated_cookie_names.update(updates)

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, object]:
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise QuarkUpstreamChangedError("Quark returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise QuarkUpstreamChangedError("Quark returned a non-object response")
        return payload

    @staticmethod
    def _raise_for_error(
        stage: str,
        http_status: int,
        payload: dict[str, object],
    ) -> None:
        raw_status = payload.get("status")
        raw_code = payload.get("code")
        business_status = _safe_int(raw_status)
        business_code = _safe_int(raw_code)
        status_ok = raw_status is None or business_status == 200
        code_ok = raw_code is None or business_code == 0
        success = http_status < 400 and status_ok and code_ok
        if success:
            return

        message = str(payload.get("message") or "").lower()
        if http_status == 429 or business_status == 429:
            raise QuarkRateLimitedError("Quark rate limited the read-only probe")
        if http_status in {401, 403} or business_status in {401, 403}:
            raise QuarkAuthExpiredError("Quark Cookie is expired or unauthorized")
        if any(marker in message for marker in ("captcha", "risk", "verify", "验证码", "风控")):
            raise QuarkRiskControlError("Quark requested additional verification")
        if stage.startswith("share"):
            if stage == "share save":
                if business_code == 41017:
                    raise QuarkWriteRejectedError(
                        "Quark rejected the share-save request because the share may "
                        "belong to the destination account; use a share created by "
                        "another account (status=404, code=41017)"
                    )
                raise QuarkWriteRejectedError(
                    "Quark rejected the share-save request "
                    f"(status={business_status}, code={business_code})"
                )
            raise QuarkShareInvalidError("Quark share is invalid, protected, or unavailable")
        if any(marker in message for marker in ("login", "cookie", "未登录", "登录")):
            raise QuarkAuthExpiredError("Quark Cookie is expired or unauthorized")
        raise QuarkProbeError("Quark rejected the read-only probe")

    @staticmethod
    def _data_object(payload: dict[str, object], stage: str) -> dict[str, object]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise QuarkUpstreamChangedError(f"Quark {stage} response contained no data object")
        return data

    @classmethod
    def _listing_result(cls, payload: dict[str, object], stage: str) -> ListingProbeResult:
        data = cls._data_object(payload, stage)
        raw_items = data.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise QuarkUpstreamChangedError(f"Quark {stage} response contained invalid items")
        items: list[dict[str, object]] = raw_items
        metadata = payload.get("metadata")
        total_count = _safe_int(metadata.get("_total")) if isinstance(metadata, dict) else None
        return ListingProbeResult(
            item_count=len(items),
            total_count=total_count,
            field_names=_field_names(items),
        )

    async def probe_account(self) -> AccountProbeResult:
        data = await self.fetch_account()
        if not any(
            field in data for field in ("member_type", "member_status", "total_capacity")
        ):
            raise QuarkUpstreamChangedError(
                "Quark account response contained no stable membership field"
            )
        return AccountProbeResult(session_accepted=True)

    async def fetch_account(self) -> dict[str, object]:
        payload = await self._request(
            "account",
            "GET",
            DRIVE_ORIGIN,
            "/1/clouddrive/member",
            params={
                "pr": "ucpro",
                "fr": "pc",
                "fetch_subscribe": "false",
                "_ch": "home",
                "fetch_identity": "false",
            },
        )
        return self._data_object(payload, "account")

    async def fetch_drive_page(
        self,
        parent_id: str,
        *,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if not parent_id or len(parent_id) > 256 or any(ord(char) < 32 for char in parent_id):
            raise ValueError("Invalid Quark Drive parent folder ID")
        if not 1 <= page <= 1_000_000:
            raise ValueError("Quark Drive page must be between 1 and 1000000")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        return await self._request(
            "root",
            "GET",
            DRIVE_ORIGIN,
            "/1/clouddrive/file/sort",
            params={
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "pdir_fid": parent_id,
                "_page": page,
                "_size": page_size,
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
                "fetch_all_file": 1,
                "fetch_risk_file_name": 1,
            },
        )

    async def probe_root(self, page_size: int = 10) -> ListingProbeResult:
        payload = await self.fetch_drive_page("0", page_size=page_size)
        return self._listing_result(payload, "root")

    async def fetch_share_token(self, share_id: str, password: str = "") -> str:
        if not SHARE_ID_PATTERN.fullmatch(share_id):
            raise ValueError("Invalid Quark Drive share ID")
        token_payload = await self._request(
            "share_token",
            "POST",
            DRIVE_ORIGIN,
            "/1/clouddrive/share/sharepage/token",
            params={"pr": "ucpro", "fr": "pc"},
            body={"pwd_id": share_id, "passcode": password},
        )
        token_data = self._data_object(token_payload, "share token")
        stoken = token_data.get("stoken")
        if not isinstance(stoken, str) or not stoken:
            raise QuarkUpstreamChangedError("Quark share response contained no stoken")
        return stoken

    async def fetch_share_page(
        self,
        share_id: str,
        stoken: str,
        parent_id: str,
        *,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if not SHARE_ID_PATTERN.fullmatch(share_id):
            raise ValueError("Invalid Quark Drive share ID")
        if not stoken or len(stoken) > 4096 or any(ord(char) < 32 for char in stoken):
            raise ValueError("Invalid Quark Drive share token")
        if not parent_id or len(parent_id) > 256 or any(ord(char) < 32 for char in parent_id):
            raise ValueError("Invalid Quark Drive share folder ID")
        if not 1 <= page <= 1_000_000:
            raise ValueError("Quark Drive page must be between 1 and 1000000")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        return await self._request(
            "share_list",
            "GET",
            DRIVE_ORIGIN,
            "/1/clouddrive/share/sharepage/detail",
            params={
                "pr": "ucpro",
                "fr": "pc",
                "pwd_id": share_id,
                "stoken": stoken,
                "pdir_fid": parent_id,
                "force": "0",
                "_page": page,
                "_size": page_size,
                "_fetch_banner": 0,
                "_fetch_share": 0,
                "_fetch_total": 1,
                "_sort": "file_type:asc,updated_at:desc",
                "ver": 2,
                "fetch_share_full_path": 0,
            },
        )

    async def probe_share(
        self,
        share_url: str,
        password: str = "",
        page_size: int = 10,
    ) -> ShareProbeResult:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        share_id = parse_share_url(share_url)
        stoken = await self.fetch_share_token(share_id, password)
        detail_payload = await self.fetch_share_page(
            share_id,
            stoken,
            "0",
            page_size=page_size,
        )
        result = self._listing_result(detail_payload, "share list")
        return ShareProbeResult(
            item_count=result.item_count,
            total_count=result.total_count,
            field_names=result.field_names,
        )

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
        root = await self.probe_root(page_size)
        share = None
        if share_url is not None:
            notify("share")
            share = await self.probe_share(share_url, share_password, page_size)
        notify("complete")
        return ReadOnlyProbeReport(
            account=account,
            root=root,
            share=share,
            rotated_cookie_names=self.rotated_cookie_names,
        )
