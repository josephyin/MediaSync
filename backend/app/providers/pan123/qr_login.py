from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage

from app.core.exceptions import ProviderRequestError
from app.providers.pan123.readonly_probe import normalize_access_token

DEFAULT_BASE_URL = "https://user.123pan.cn"
LOGIN_PAGE_PATH = (
    "/centerlogin?redirect_url=https%3A%2F%2Fyun.123pan.cn&source_page=website"
)
ALLOWED_QR_HOSTS = frozenset({"yun.123pan.cn", "www.123pan.cn"})


@dataclass(slots=True)
class Pan123QrLoginSession:
    session_id: str
    uni_id: str
    login_uuid: str
    account_id: int | None
    account_name: str | None
    expires_at: datetime
    http_client: httpx.AsyncClient
    access_token: str | None = None
    was_scanned: bool = False


@dataclass(frozen=True, slots=True)
class Pan123QrLoginStart:
    session_id: str
    qr_code_data_url: str
    expires_in: int


class Pan123QrLogin:
    """In-memory 123 Cloud Drive Web QR login session manager."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        http_client: httpx.AsyncClient | None = None,
        session_ttl_seconds: int = 120,
    ) -> None:
        parsed = urlparse(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("123 QR login base URL must be a plain HTTPS origin")
        self.base_url = base_url.rstrip("/")
        self._injected_http_client = http_client
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, Pan123QrLoginSession] = {}

    @staticmethod
    def _headers(login_uuid: str) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://user.123pan.cn",
            "Referer": (
                "https://user.123pan.cn/centerlogin?"
                "redirect_url=https%3A%2F%2Fyun.123pan.cn&source_page=website"
            ),
            "Platform": "web",
            "App-Version": "132",
            "LoginUuid": login_uuid,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        }

    @staticmethod
    def _qr_svg_data_url(content: str) -> str:
        qr = qrcode.QRCode(box_size=7, border=2)
        qr.add_data(content)
        qr.make(fit=True)
        image = qr.make_image(image_factory=SvgPathImage)
        output = BytesIO()
        image.save(output)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/svg+xml;base64,{encoded}"

    @staticmethod
    def _qr_url(base_url: str, uni_id: str) -> str:
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in ALLOWED_QR_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ProviderRequestError("123 Cloud Drive returned an invalid QR login URL")
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update({"uniID": uni_id, "env": "production"})
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _data(payload: dict[str, object], stage: str) -> dict[str, object]:
        code = payload.get("code")
        if code not in {0, 200}:
            raise ProviderRequestError(f"123 Cloud Drive rejected the QR {stage} request")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProviderRequestError(f"123 Cloud Drive QR {stage} response has no data")
        return data

    async def _new_client(self) -> httpx.AsyncClient:
        if self._injected_http_client is not None:
            return self._injected_http_client
        return httpx.AsyncClient(timeout=15, follow_redirects=True)

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        method: str,
        login_uuid: str,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], httpx.Response]:
        try:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(login_uuid),
                params=params,
                json=body,
            )
        except httpx.HTTPError as exc:
            raise ProviderRequestError("123 Cloud Drive QR login request failed") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"123 Cloud Drive QR login returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if response.is_error or not isinstance(payload, dict):
            raise ProviderRequestError(
                f"123 Cloud Drive QR login failed (HTTP {response.status_code})"
            )
        return payload, response

    async def start(
        self, *, account_id: int | None = None, account_name: str | None = None
    ) -> Pan123QrLoginStart:
        await self._discard_expired()
        client = await self._new_client()
        owns_client = self._injected_http_client is None
        login_uuid = secrets.token_hex(32)
        try:
            try:
                await client.get(
                    f"{self.base_url}{LOGIN_PAGE_PATH}",
                    headers={"User-Agent": self._headers(login_uuid)["User-Agent"]},
                )
            except httpx.HTTPError:
                # The QR endpoint can still succeed when the optional warm-up
                # page is blocked by a CDN challenge.
                pass
            payload, _ = await self._request_json(
                client,
                "/api/user/qr-code/generate",
                method="GET",
                login_uuid=login_uuid,
                params={"uniID": str(uuid.uuid4())},
            )
            data = self._data(payload, "generate")
            uni_id = data.get("uniID")
            qr_base_url = data.get("url")
            if not isinstance(uni_id, str) or not uni_id or not isinstance(qr_base_url, str):
                raise ProviderRequestError("123 Cloud Drive returned an invalid QR session")
            qr_url = self._qr_url(qr_base_url, uni_id)
        except Exception:
            if owns_client:
                await client.aclose()
            raise

        session_id = secrets.token_urlsafe(24)
        self._sessions[session_id] = Pan123QrLoginSession(
            session_id=session_id,
            uni_id=uni_id,
            login_uuid=login_uuid,
            account_id=account_id,
            account_name=account_name,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.session_ttl_seconds),
            http_client=client,
        )
        return Pan123QrLoginStart(
            session_id=session_id,
            qr_code_data_url=self._qr_svg_data_url(qr_url),
            expires_in=self.session_ttl_seconds,
        )

    def get_session(self, session_id: str) -> Pan123QrLoginSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("QR login session not found")
        if datetime.now(UTC) >= session.expires_at:
            raise ValueError("QR code has expired")
        return session

    async def _wechat_code(self, session: Pan123QrLoginSession) -> str | None:
        payload, _ = await self._request_json(
            session.http_client,
            "/api/user/qr-code/wx_code",
            method="POST",
            login_uuid=session.login_uuid,
            body={"uniID": session.uni_id},
        )
        # The endpoint can briefly return a non-success code after the QR page
        # reports "scanned" but before the user confirms WeChat authorization.
        # Treat that narrow transition as pending instead of failing the whole
        # browser poll loop.
        if payload.get("code") not in {0, 200}:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        value = data.get("wxCode") or data.get("wechat_code")
        return value if isinstance(value, str) and value else None

    async def _exchange_token(self, session: Pan123QrLoginSession, code: str) -> str:
        payload, response = await self._request_json(
            session.http_client,
            "/api/user/sign_in",
            method="POST",
            login_uuid=session.login_uuid,
            body={
                "from": "web",
                "wechat_code": code,
                "type": 4,
                "remember": True,
                "gray": True,
            },
        )
        data = self._data(payload, "sign-in")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            token = response.cookies.get("sso-token")
        if not token:
            raise ProviderRequestError("123 Cloud Drive QR sign-in returned no access token")
        try:
            return normalize_access_token(token)
        except ValueError as exc:
            raise ProviderRequestError("123 Cloud Drive returned an invalid access token") from exc

    async def poll(self, session_id: str) -> tuple[str, Pan123QrLoginSession]:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("QR login session not found")
        if datetime.now(UTC) >= session.expires_at:
            await self.finish(session_id)
            raise ValueError("QR code has expired")
        if session.access_token:
            return "confirmed", session
        payload, _ = await self._request_json(
            session.http_client,
            "/api/user/qr-code/result",
            method="GET",
            login_uuid=session.login_uuid,
            params={"uniID": session.uni_id, "remember": "true", "gray": "true"},
        )
        data = self._data(payload, "status")
        login_status = data.get("loginStatus")
        if isinstance(login_status, str) and login_status.isdecimal():
            login_status = int(login_status)
        if login_status not in {0, 1, 2, 3, 4}:
            raise ProviderRequestError("123 Cloud Drive returned an invalid QR login status")
        if login_status in {3, 4}:
            # 123 briefly reports expired after the mobile authorization page
            # closes while wx_code may already be available. Recover it once
            # when this session was observed as scanned before declaring failure.
            if session.was_scanned:
                code = await self._wechat_code(session)
                if code:
                    session.access_token = await self._exchange_token(session, code)
                    return "confirmed", session
            await self.finish(session_id)
            return "expired", session
        if login_status == 0:
            return "waiting", session

        session.was_scanned = True
        code = await self._wechat_code(session)
        if not code:
            return "scanned", session
        session.access_token = await self._exchange_token(session, code)
        return "confirmed", session

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
