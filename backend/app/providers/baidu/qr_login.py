from __future__ import annotations

import asyncio
import base64
import http.client
import json
import re
import secrets
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse

import httpx

from app.core.exceptions import ProviderRequestError
from app.providers.baidu.share_probe import normalize_cookie

PASSPORT_ORIGIN = "https://passport.baidu.com"
QR_GENERATE_PATH = "/v2/api/getqrcode"
QR_STATUS_PATH = "/channel/unicast"
QR_EXCHANGE_PATH = "/v3/login/main/qrbdusslogin"
SIGN_PATTERN = re.compile(r"^[a-fA-F0-9]{16,128}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_QR_IMAGE_BYTES = 256 * 1024
MAX_TEMP_BDUSS_LENGTH = 4096


@dataclass(slots=True)
class BaiduQrLoginSession:
    session_id: str
    sign: str
    account_id: int | None
    account_name: str | None
    expires_at: datetime
    http_client: httpx.AsyncClient
    cookie: str | None = None


@dataclass(frozen=True, slots=True)
class BaiduQrLoginStart:
    session_id: str
    qr_code_data_url: str
    expires_in: int


class BaiduQrLogin:
    """In-memory Baidu Passport QR login manager for a private BDUSS session."""

    def __init__(
        self,
        base_url: str = PASSPORT_ORIGIN,
        *,
        http_client: httpx.AsyncClient | None = None,
        credential_exchanger: Callable[[str], str] | None = None,
        session_ttl_seconds: int = 300,
    ) -> None:
        parsed = urlparse(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Baidu QR login base URL must be a plain HTTPS origin")
        self.base_url = base_url.rstrip("/")
        self._injected_http_client = http_client
        self._credential_exchanger = credential_exchanger or self._exchange_cookie_sync
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, BaiduQrLoginSession] = {}

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://pan.baidu.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }

    async def _new_client(self) -> httpx.AsyncClient:
        if self._injected_http_client is not None:
            return self._injected_http_client
        return httpx.AsyncClient(timeout=35, follow_redirects=False)

    async def _request(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, str],
    ) -> httpx.Response:
        if path not in {QR_GENERATE_PATH, QR_STATUS_PATH}:
            raise ProviderRequestError("Baidu QR login refused an invalid fixed path")
        try:
            return await client.get(
                f"{self.base_url}{path}",
                params=params,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ProviderRequestError("Baidu QR login request failed") from exc

    @staticmethod
    def _json_object(response: httpx.Response, stage: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Baidu QR {stage} returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if response.is_error or not isinstance(payload, dict):
            raise ProviderRequestError(
                f"Baidu QR {stage} failed (HTTP {response.status_code})"
            )
        return payload

    @staticmethod
    def _qr_image_url(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ProviderRequestError("Baidu returned an empty QR image URL")
        raw = value.strip()
        if not raw.startswith(("http://", "https://")):
            raw = f"https://{raw.lstrip('/')}"
        parsed = urlparse(raw)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "passport.baidu.com"
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.path != QR_GENERATE_PATH.replace("getqrcode", "qrcode")
        ):
            raise ProviderRequestError("Baidu returned an invalid QR image URL")
        return raw

    async def _qr_image_data_url(
        self, client: httpx.AsyncClient, image_url: str
    ) -> str:
        try:
            response = await client.get(image_url, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderRequestError("Baidu QR image request failed") from exc
        image = response.content
        if response.is_error or not image.startswith(PNG_SIGNATURE):
            raise ProviderRequestError("Baidu returned an invalid QR image")
        if len(image) > MAX_QR_IMAGE_BYTES:
            raise ProviderRequestError("Baidu returned an oversized QR image")
        encoded = base64.b64encode(image).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    async def start(
        self, *, account_id: int | None = None, account_name: str | None = None
    ) -> BaiduQrLoginStart:
        await self._discard_expired()
        client = await self._new_client()
        owns_client = self._injected_http_client is None
        try:
            response = await self._request(
                client,
                QR_GENERATE_PATH,
                params={"lp": "pc"},
            )
            payload = self._json_object(response, "generate")
            if payload.get("errno") != 0:
                raise ProviderRequestError("Baidu rejected the QR generate request")
            sign = payload.get("sign")
            if not isinstance(sign, str) or not SIGN_PATTERN.fullmatch(sign):
                raise ProviderRequestError("Baidu returned an invalid QR session")
            image_url = self._qr_image_url(payload.get("imgurl"))
            image_data_url = await self._qr_image_data_url(client, image_url)
        except Exception:
            if owns_client:
                await client.aclose()
            raise

        session_id = secrets.token_urlsafe(24)
        self._sessions[session_id] = BaiduQrLoginSession(
            session_id=session_id,
            sign=sign,
            account_id=account_id,
            account_name=account_name,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.session_ttl_seconds),
            http_client=client,
        )
        return BaiduQrLoginStart(
            session_id=session_id,
            qr_code_data_url=image_data_url,
            expires_in=self.session_ttl_seconds,
        )

    @staticmethod
    def _status_payload(response: httpx.Response) -> dict[str, object] | None:
        if response.is_error:
            raise ProviderRequestError(
                f"Baidu QR status failed (HTTP {response.status_code})"
            )
        text = response.text.strip()
        if not text:
            return None
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1].strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderRequestError("Baidu QR status returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ProviderRequestError("Baidu QR status returned an invalid response")
        return payload

    @staticmethod
    def _channel_data(value: object) -> dict[str, object] | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProviderRequestError("Baidu QR status data is invalid") from exc
        if not isinstance(data, dict):
            raise ProviderRequestError("Baidu QR status data has an invalid shape")
        return data

    async def poll(self, session_id: str) -> tuple[str, BaiduQrLoginSession]:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("QR login session not found")
        if datetime.now(UTC) >= session.expires_at:
            await self.finish(session_id)
            return "expired", session
        if session.cookie:
            return "confirmed", session

        response = await self._request(
            session.http_client,
            QR_STATUS_PATH,
            params={"channel_id": session.sign, "callback": ""},
        )
        payload = self._status_payload(response)
        if payload is None or payload.get("errno") != 0:
            return "waiting", session
        channel_data = self._channel_data(payload.get("channel_v"))
        if channel_data is None:
            return "waiting", session
        status = channel_data.get("status")
        temp_bduss = channel_data.get("v")
        if status == 0 and isinstance(temp_bduss, str) and temp_bduss:
            if (
                len(temp_bduss) > MAX_TEMP_BDUSS_LENGTH
                or any(ord(character) < 33 or ord(character) == 127 for character in temp_bduss)
            ):
                raise ProviderRequestError("Baidu returned an invalid temporary credential")
            try:
                cookie = await asyncio.to_thread(self._credential_exchanger, temp_bduss)
                session.cookie = normalize_cookie(cookie)
            except (OSError, ValueError) as exc:
                raise ProviderRequestError("Baidu QR credential exchange failed") from exc
            return "confirmed", session
        return "scanned", session

    @staticmethod
    def _exchange_cookie_sync(temp_bduss: str) -> str:
        # Keep this sensitive value out of httpx's request logger: use a fixed
        # TLS host/path and the standard library for this one credential-bearing request.
        path = f"{QR_EXCHANGE_PATH}?bduss={quote(temp_bduss, safe='')}"
        connection = http.client.HTTPSConnection(
            "passport.baidu.com",
            timeout=20,
            context=ssl.create_default_context(),
        )
        try:
            connection.request("GET", path, headers=BaiduQrLogin._headers())
            response = connection.getresponse()
            response.read(64 * 1024)
            set_cookie_headers = [
                value for name, value in response.getheaders() if name.lower() == "set-cookie"
            ]
        finally:
            connection.close()
        for header in set_cookie_headers:
            match = re.search(r"(?:^|;\s*)BDUSS=([^;]+)", header)
            if match:
                return f"BDUSS={match.group(1)}"
        raise ValueError("Baidu QR login returned no BDUSS cookie")

    async def finish(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None and self._injected_http_client is None:
            await session.http_client.aclose()

    async def _discard_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now >= session.expires_at
        ]
        for session_id in expired:
            await self.finish(session_id)
