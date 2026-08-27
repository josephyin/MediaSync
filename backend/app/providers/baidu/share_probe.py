from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx

from app.core.exceptions import ProviderRequestError

PAN_ORIGIN = "https://pan.baidu.com"
TIEBA_ORIGIN = "https://tieba.baidu.com"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_COOKIE_LENGTH = 32_768
MAX_PAGE_SIZE = 100
COOKIE_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
SHARE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,128}$")


class BaiduShareProbeError(ProviderRequestError):
    code = "BAIDU_SHARE_PROBE_FAILED"


class BaiduCookieExpiredError(BaiduShareProbeError):
    code = "BAIDU_COOKIE_EXPIRED"


class BaiduShareInvalidError(BaiduShareProbeError):
    code = "BAIDU_SHARE_INVALID"


class BaiduShareRateLimitedError(BaiduShareProbeError):
    code = "BAIDU_RATE_LIMITED"


class BaiduShareRiskControlError(BaiduShareProbeError):
    code = "BAIDU_RISK_CONTROL"


class BaiduShareUpstreamChangedError(BaiduShareProbeError):
    code = "BAIDU_UPSTREAM_CHANGED"


@dataclass(frozen=True, slots=True)
class AccountProbeResult:
    session_accepted: bool


@dataclass(frozen=True, slots=True)
class ShareProbeResult:
    item_count: int
    total_count: int | None
    field_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShareReadOnlyProbeReport:
    account: AccountProbeResult
    share: ShareProbeResult


def normalize_cookie(raw_cookie: str) -> str:
    if not isinstance(raw_cookie, str):
        raise ValueError("Cookie must be a string")
    value = raw_cookie.strip()
    if not value:
        raise ValueError("Cookie is required")
    if value.lower().startswith("cookie:"):
        value = value[7:].strip()
    if len(value) > MAX_COOKIE_LENGTH:
        raise ValueError("Cookie is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Cookie contains control characters")

    # Browser cookie tables commonly copy only the selected BDUSS value. Accept
    # that unambiguous form so users do not have to reconstruct a credential.
    if ";" not in value and not value.startswith("BDUSS="):
        if len(value) < 20 or any(character.isspace() for character in value):
            raise ValueError("Cookie contains an invalid item")
        return f"BDUSS={value}"

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
    if not parsed.get("BDUSS"):
        raise ValueError("Cookie must contain a non-empty BDUSS value")
    return "; ".join(f"{name}={cookie_value}" for name, cookie_value in parsed.items())


def parse_share_url(share_url: str) -> tuple[str, str | None]:
    parsed = urlparse(share_url.strip())
    if parsed.scheme != "https" or parsed.hostname != "pan.baidu.com":
        raise ValueError("Share URL must use https://pan.baidu.com")
    if parsed.username or parsed.password or parsed.port:
        raise ValueError("Share URL cannot contain credentials or a custom port")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "s" or not SHARE_ID_PATTERN.fullmatch(parts[1]):
        raise ValueError("Invalid Baidu Netdisk share URL")
    passwords = parse_qs(parsed.query).get("pwd", [])
    if len(passwords) > 1:
        raise ValueError("Share URL contains more than one password")
    password = passwords[0] if passwords else None
    if password and (len(password) > 32 or any(character.isspace() for character in password)):
        raise ValueError("Share password in URL is invalid")
    return parts[1], password


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


class BaiduShareReadOnlyProbe:
    """Read-only, non-persistent probe for Baidu Web session and share listing."""

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
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self.request_count = 0

    async def __aenter__(self) -> BaiduShareReadOnlyProbe:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _headers(self, *, form: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Cookie": self._cookie,
            "User-Agent": "netdisk",
        }
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Origin"] = PAN_ORIGIN
            headers["Referer"] = f"{PAN_ORIGIN}/"
        return headers

    async def _request(
        self,
        stage: str,
        method: Literal["GET", "POST"],
        origin: Literal["https://pan.baidu.com", "https://tieba.baidu.com"],
        path: str,
        *,
        params: dict[str, object] | None = None,
        form: dict[str, object] | None = None,
    ) -> dict[str, object]:
        allowed = {
            (TIEBA_ORIGIN, "/mo/q/sync"),
            (PAN_ORIGIN, "/share/wxlist"),
        }
        if (origin, path) not in allowed:
            raise BaiduShareUpstreamChangedError("Baidu share probe refused an invalid fixed URL")
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
                    data=form,
                    headers=self._headers(form=form is not None),
                )
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
                    continue
                label = "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
                raise BaiduShareProbeError(f"Baidu {stage} request {label}") from exc
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                await asyncio.sleep(self._retry_backoff_seconds * (2**attempt))
                continue
            break
        assert response is not None
        if response.is_redirect:
            error_type = (
                BaiduShareInvalidError
                if stage == "share listing"
                else BaiduCookieExpiredError
            )
            raise error_type(f"Baidu redirected the {stage} probe")
        if response.status_code in {401, 403}:
            raise BaiduCookieExpiredError("Baidu Cookie is expired or unauthorized")
        if response.status_code == 429:
            raise BaiduShareRateLimitedError("Baidu rate limited the read-only probe")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise BaiduShareUpstreamChangedError(
                f"Baidu {stage} returned invalid JSON "
                f"(http_status={response.status_code}, content_type={content_type or 'unknown'})"
            ) from exc
        if not isinstance(payload, dict):
            raise BaiduShareUpstreamChangedError(
                f"Baidu {stage} returned an unexpected response shape"
            )
        self._raise_for_error(stage, response.status_code, payload)
        return payload

    @staticmethod
    def _raise_for_error(
        stage: str,
        http_status: int,
        payload: dict[str, object],
    ) -> None:
        errno = _safe_int(payload.get("errno"))
        if http_status < 400 and errno in (None, 0):
            return
        message = str(payload.get("errmsg") or payload.get("show_msg") or "").lower()
        if http_status == 429:
            raise BaiduShareRateLimitedError("Baidu rate limited the read-only probe")
        if http_status in {401, 403} or errno in {-6, 110, 111}:
            raise BaiduCookieExpiredError("Baidu Cookie is expired or unauthorized")
        if any(marker in message for marker in ("captcha", "verify", "risk", "验证码", "风控")):
            raise BaiduShareRiskControlError("Baidu requested additional verification")
        if stage == "share listing":
            raise BaiduShareInvalidError(
                "Baidu share is invalid, protected, or unavailable "
                f"(http_status={http_status}, errno={errno})"
            )
        raise BaiduShareProbeError(
            f"Baidu rejected the {stage} request "
            f"(http_status={http_status}, errno={errno})"
        )

    async def probe_account(self) -> AccountProbeResult:
        payload = await self._request(
            "Cookie account",
            "GET",
            TIEBA_ORIGIN,
            "/mo/q/sync",
        )
        data = payload.get("data")
        user_id = data.get("user_id") if isinstance(data, dict) else None
        if _safe_int(user_id) in (None, 0):
            raise BaiduShareUpstreamChangedError(
                "Baidu Cookie account returned an unexpected response shape"
            )
        return AccountProbeResult(session_accepted=True)

    async def probe_share(
        self,
        share_url: str,
        *,
        password: str = "",
        page_size: int = 10,
    ) -> ShareProbeResult:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        share_id, url_password = parse_share_url(share_url)
        if password and url_password and password != url_password:
            raise ValueError("Share password conflicts with the password in the URL")
        effective_password = password or url_password or ""
        payload = await self._request(
            "share listing",
            "POST",
            PAN_ORIGIN,
            "/share/wxlist",
            params={
                "channel": "weixin",
                "version": "2.2.2",
                "clienttype": 25,
                "web": 1,
            },
            form={
                "pwd": effective_password,
                "root": "1",
                "shorturl": share_id,
                "num": page_size,
                "order": "time",
                "page": 1,
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BaiduShareUpstreamChangedError(
                "Baidu share listing returned an unexpected response shape"
            )
        raw_items = data.get("list")
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
            raise BaiduShareUpstreamChangedError(
                "Baidu share listing returned invalid items"
            )
        items: list[dict[str, object]] = raw_items
        return ShareProbeResult(
            item_count=len(items),
            total_count=None,
            field_names=_field_names(items),
        )

    async def run(
        self,
        *,
        share_url: str,
        password: str = "",
        page_size: int = 10,
        progress: Callable[[str], None] | None = None,
    ) -> ShareReadOnlyProbeReport:
        if progress:
            progress("account")
        account = await self.probe_account()
        if progress:
            progress("share")
        share = await self.probe_share(
            share_url,
            password=password,
            page_size=page_size,
        )
        if progress:
            progress("complete")
        return ShareReadOnlyProbeReport(account=account, share=share)
