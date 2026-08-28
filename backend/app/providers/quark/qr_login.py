from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.parse import urlencode, urlparse

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage

from app.core.exceptions import ProviderRequestError
from app.providers.quark.readonly_probe import normalize_cookie

UOP_ORIGIN = "https://uop.quark.cn"
PAN_ORIGIN = "https://pan.quark.cn"
QR_TOKEN_PATH = "/cas/ajax/getTokenForQrcodeLogin"
QR_STATUS_PATH = "/cas/ajax/getServiceTicketByQrcodeToken"
ACCOUNT_INFO_PATH = "/account/info"
QR_LOGIN_URL = "https://su.quark.cn/4_eMHBJ"
QR_CLIENT_ID = "532"
SUCCESS_STATUS = 2_000_000
WAITING_STATUSES = frozenset({5_000_4000, 5_000_4001})
EXPIRED_STATUSES = frozenset({5_000_4002, 5_000_4003, 5_000_4004})
MAX_TOKEN_LENGTH = 4096


@dataclass(slots=True)
class QuarkQrLoginSession:
    session_id: str
    qr_token: str
    account_id: int | None
    account_name: str | None
    expires_at: datetime
    http_client: httpx.AsyncClient
    cookie: str | None = None


@dataclass(frozen=True, slots=True)
class QuarkQrLoginStart:
    session_id: str
    qr_code_data_url: str
    expires_in: int


class QuarkQrLogin:
    """In-memory Quark Web QR login manager that yields a private Cookie."""

    def __init__(
        self,
        *,
        uop_origin: str = UOP_ORIGIN,
        pan_origin: str = PAN_ORIGIN,
        http_client: httpx.AsyncClient | None = None,
        session_ttl_seconds: int = 300,
    ) -> None:
        self.uop_origin = self._official_origin(
            uop_origin, "Quark UOP", "uop.quark.cn"
        )
        self.pan_origin = self._official_origin(
            pan_origin, "Quark Pan", "pan.quark.cn"
        )
        self._injected_http_client = http_client
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, QuarkQrLoginSession] = {}

    @staticmethod
    def _official_origin(value: str, label: str, expected_hostname: str) -> str:
        parsed = urlparse(value.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname != expected_hostname
            or parsed.username
            or parsed.password
            or parsed.port
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(f"{label} origin must use its official HTTPS hostname")
        return value.rstrip("/")

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{PAN_ORIGIN}/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
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
    def _payload(response: httpx.Response, stage: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Quark QR {stage} returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if response.is_error or not isinstance(payload, dict):
            raise ProviderRequestError(
                f"Quark QR {stage} failed (HTTP {response.status_code})"
            )
        return payload

    @staticmethod
    def _status(payload: dict[str, object], stage: str) -> int:
        status = payload.get("status")
        if isinstance(status, str) and status.isdecimal():
            status = int(status)
        if not isinstance(status, int):
            raise ProviderRequestError(f"Quark QR {stage} returned an invalid status")
        return status

    @staticmethod
    def _members(payload: dict[str, object], stage: str) -> dict[str, object]:
        data = payload.get("data")
        members = data.get("members") if isinstance(data, dict) else None
        if not isinstance(members, dict):
            raise ProviderRequestError(f"Quark QR {stage} response has no members")
        return members

    async def _new_client(self) -> httpx.AsyncClient:
        if self._injected_http_client is not None:
            return self._injected_http_client
        return httpx.AsyncClient(timeout=20, follow_redirects=True, headers=self._headers())

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        origin: str,
        path: str,
        *,
        params: dict[str, str],
        stage: str,
    ) -> dict[str, object]:
        if (origin, path) not in {
            (self.uop_origin, QR_TOKEN_PATH),
            (self.uop_origin, QR_STATUS_PATH),
        }:
            raise ProviderRequestError("Quark QR login refused an invalid fixed path")
        try:
            response = await client.get(
                f"{origin}{path}", params=params, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderRequestError("Quark QR login request failed") from exc
        return self._payload(response, stage)

    @staticmethod
    def _qr_url(token: str) -> str:
        params = {
            "token": token,
            "client_id": QR_CLIENT_ID,
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": (
                "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0"
            ),
        }
        return f"{QR_LOGIN_URL}?{urlencode(params)}"

    async def start(
        self, *, account_id: int | None = None, account_name: str | None = None
    ) -> QuarkQrLoginStart:
        await self._discard_expired()
        client = await self._new_client()
        owns_client = self._injected_http_client is None
        try:
            payload = await self._get_json(
                client,
                self.uop_origin,
                QR_TOKEN_PATH,
                params={
                    "client_id": QR_CLIENT_ID,
                    "v": "1.2",
                    "request_id": str(uuid.uuid4()),
                },
                stage="generate",
            )
            if self._status(payload, "generate") != SUCCESS_STATUS:
                raise ProviderRequestError("Quark rejected the QR generate request")
            token = self._members(payload, "generate").get("token")
            if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
                raise ProviderRequestError("Quark returned an invalid QR login token")
        except Exception:
            if owns_client:
                await client.aclose()
            raise

        session_id = secrets.token_urlsafe(24)
        self._sessions[session_id] = QuarkQrLoginSession(
            session_id=session_id,
            qr_token=token,
            account_id=account_id,
            account_name=account_name,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.session_ttl_seconds),
            http_client=client,
        )
        return QuarkQrLoginStart(
            session_id=session_id,
            qr_code_data_url=self._qr_svg_data_url(self._qr_url(token)),
            expires_in=self.session_ttl_seconds,
        )

    async def _exchange_cookie(
        self, session: QuarkQrLoginSession, service_ticket: str
    ) -> str:
        if not service_ticket or len(service_ticket) > MAX_TOKEN_LENGTH:
            raise ProviderRequestError("Quark returned an invalid QR service ticket")
        try:
            response = await session.http_client.get(
                f"{self.pan_origin}{ACCOUNT_INFO_PATH}",
                params={"st": service_ticket, "lw": "scan"},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise ProviderRequestError("Quark QR credential exchange failed") from exc
        if response.is_error:
            raise ProviderRequestError(
                f"Quark QR credential exchange failed (HTTP {response.status_code})"
            )
        cookie_values = {
            cookie.name: cookie.value
            for cookie in session.http_client.cookies.jar
            if cookie.domain and cookie.domain.lstrip(".").endswith("quark.cn")
        }
        values = [f"{name}={value}" for name, value in cookie_values.items()]
        try:
            cookie = normalize_cookie("; ".join(values))
        except ValueError as exc:
            raise ProviderRequestError("Quark QR login returned no usable Cookie") from exc
        required = {"__pus", "__kps"}
        names = {item.split("=", 1)[0] for item in cookie.split("; ")}
        if not required.issubset(names):
            raise ProviderRequestError("Quark QR login returned an incomplete Cookie")
        return cookie

    async def poll(self, session_id: str) -> tuple[str, QuarkQrLoginSession]:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("QR login session not found")
        if datetime.now(UTC) >= session.expires_at:
            await self.finish(session_id)
            return "expired", session
        if session.cookie:
            return "confirmed", session
        payload = await self._get_json(
            session.http_client,
            self.uop_origin,
            QR_STATUS_PATH,
            params={
                "client_id": QR_CLIENT_ID,
                "v": "1.2",
                "token": session.qr_token,
                "request_id": str(uuid.uuid4()),
            },
            stage="status",
        )
        status = self._status(payload, "status")
        if status in WAITING_STATUSES:
            return "waiting", session
        if status in EXPIRED_STATUSES:
            await self.finish(session_id)
            return "expired", session
        if status != SUCCESS_STATUS:
            raise ProviderRequestError("Quark rejected the QR status request")
        service_ticket = self._members(payload, "status").get("service_ticket")
        if not isinstance(service_ticket, str) or not service_ticket:
            raise ProviderRequestError("Quark QR confirmation contained no service ticket")
        session.cookie = await self._exchange_cookie(session, service_ticket)
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
