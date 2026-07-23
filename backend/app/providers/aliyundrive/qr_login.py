import base64
import binascii
import gzip
import html
import json
import logging
import secrets
import zlib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from urllib.parse import unquote

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage

from app.core.exceptions import ProviderRequestError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QrLoginSession:
    session_id: str
    t: int
    ck: str
    code_content: str
    result_code: int
    account_id: int | None
    account_name: str | None
    expires_at: datetime
    refresh_token: str | None = None


@dataclass(slots=True)
class QrLoginStart:
    session_id: str
    qr_code_data_url: str
    expires_in: int


class AliyunDriveQrLogin:
    """Local Aliyun Drive Web QR login session manager."""

    def __init__(
        self,
        base_url: str = "https://passport.aliyundrive.com",
        http_client: httpx.AsyncClient | None = None,
        session_ttl_seconds: int = 180,
    ):
        self.base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, QrLoginSession] = {}

    @staticmethod
    def _form() -> dict[str, str]:
        return {
            "appName": "aliyun_drive",
            "fromSite": "52",
            "appEntrance": "web",
            "isMobile": "false",
            "lang": "zh_CN",
            "returnUrl": "",
            "bizParams": "",
        }

    async def _request(
        self,
        path: str,
        *,
        method: str,
        params: dict[str, object],
        form: dict[str, object] | None = None,
    ) -> dict[str, object]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.alipan.com",
            "Referer": "https://www.alipan.com/",
            "User-Agent": "Mozilla/5.0 MediaSync/0.1",
        }
        if self._http_client is None:
            # The passport endpoints bind the QR session to cookies. Reuse one
            # client for generate/query so production behaves like a browser.
            self._http_client = httpx.AsyncClient(timeout=20, follow_redirects=True)
        client = self._http_client
        try:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                data=form,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ProviderRequestError(f"Aliyun Drive QR login request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRequestError(
                f"Aliyun Drive QR login returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        if response.is_error or not isinstance(payload, dict):
            raise ProviderRequestError(
                f"Aliyun Drive QR login failed (HTTP {response.status_code})"
            )
        return payload

    @staticmethod
    def _response_data(payload: dict[str, object]) -> dict[str, object]:
        content = payload.get("content")
        if not isinstance(content, dict):
            raise ProviderRequestError("Aliyun Drive QR login response has no content")
        data = content.get("data")
        if not isinstance(data, dict):
            raise ProviderRequestError("Aliyun Drive QR login response has no data")
        return data

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

    async def start(
        self, *, account_id: int | None = None, account_name: str | None = None
    ) -> QrLoginStart:
        payload = await self._request(
            "/newlogin/qrcode/generate.do",
            method="GET",
            params={**self._form(), "_bx-v": "2.0.31"},
        )
        data = self._response_data(payload)
        try:
            t = int(data["t"])
            ck = str(data["ck"])
            code_content = str(data["codeContent"])
            result_code = int(data.get("resultCode", 100))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderRequestError("Aliyun Drive returned an invalid QR login session") from exc
        if not ck or not code_content:
            raise ProviderRequestError("Aliyun Drive returned an empty QR code")
        session_id = secrets.token_urlsafe(24)
        self._sessions[session_id] = QrLoginSession(
            session_id=session_id,
            t=t,
            ck=ck,
            code_content=code_content,
            result_code=result_code,
            account_id=account_id,
            account_name=account_name,
            expires_at=datetime.now(UTC) + timedelta(seconds=self.session_ttl_seconds),
        )
        self._discard_expired()
        return QrLoginStart(
            session_id=session_id,
            qr_code_data_url=self._qr_svg_data_url(code_content),
            expires_in=self.session_ttl_seconds,
        )

    def get_session(self, session_id: str) -> QrLoginSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("QR login session not found")
        if datetime.now(UTC) >= session.expires_at:
            self._sessions.pop(session_id, None)
            raise ValueError("QR code has expired")
        return session

    @classmethod
    def _find_refresh_token(cls, value: object) -> str | None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower().replace("_", "") == "refreshtoken" and child:
                    return str(child)
            for child in value.values():
                found = cls._find_refresh_token(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_refresh_token(child)
                if found:
                    return found
        elif isinstance(value, str):
            text = unquote(value).strip()
            if text.startswith(("{", "[")):
                try:
                    return cls._find_refresh_token(json.loads(text))
                except json.JSONDecodeError:
                    return None
        return None

    @staticmethod
    def _decoded_texts(value: bytes) -> list[str]:
        texts: list[str] = []
        for encoding in ("utf-8-sig", "utf-16", "gb18030"):
            try:
                text = value.decode(encoding).strip("\x00\ufeff \r\n\t")
            except (UnicodeDecodeError, LookupError):
                continue
            if text and text not in texts:
                texts.append(text)
        return texts

    @classmethod
    def _decode_token(cls, data: dict[str, object]) -> str:
        direct = cls._find_refresh_token(data)
        if direct:
            return direct
        biz_ext = data.get("bizExt")
        if biz_ext in (None, ""):
            raise ProviderRequestError("Aliyun Drive QR confirmation contained no login result")

        # Upstream has returned JSON, URL-encoded JSON, nested Base64 and
        # occasionally compressed payloads. Walk a bounded decode graph so a
        # format variation does not break local QR login.
        pending: deque[object] = deque([biz_ext])
        seen_text: set[str] = set()
        seen_bytes: set[bytes] = set()
        for _ in range(64):
            if not pending:
                break
            value = pending.popleft()
            token = cls._find_refresh_token(value)
            if token:
                return token
            if isinstance(value, (dict, list)):
                children = value.values() if isinstance(value, dict) else value
                pending.extend(children)
                continue
            if isinstance(value, bytes):
                if value in seen_bytes:
                    continue
                seen_bytes.add(value)
                pending.extend(cls._decoded_texts(value))
                for decompressor in (gzip.decompress, zlib.decompress):
                    try:
                        pending.append(decompressor(value))
                    except (OSError, zlib.error):
                        continue
                continue
            if not isinstance(value, str):
                continue

            variants = [
                value.strip(),
                unquote(value).strip(),
                html.unescape(value).strip(),
            ]
            for text in variants:
                if not text or text in seen_text:
                    continue
                seen_text.add(text)
                try:
                    pending.append(json.loads(text))
                except json.JSONDecodeError:
                    pass
                compact = "".join(text.split())
                padded = compact + "=" * (-len(compact) % 4)
                for decoder in (base64.b64decode, base64.urlsafe_b64decode):
                    try:
                        decoded = decoder(padded)
                    except (binascii.Error, ValueError):
                        continue
                    if decoded:
                        pending.append(decoded)
                # JWT-like wrappers sometimes carry the login object in the
                # middle segment.
                parts = text.split(".")
                if len(parts) == 3:
                    pending.append(parts[1])

        logger.warning(
            "Aliyun QR login payload decode failed: data_keys=%s biz_ext_type=%s "
            "biz_ext_length=%s decoded_texts=%d decoded_bytes=%d",
            sorted(data.keys()),
            type(biz_ext).__name__,
            len(biz_ext) if isinstance(biz_ext, (str, bytes, list, dict)) else None,
            len(seen_text),
            len(seen_bytes),
        )
        raise ProviderRequestError("Aliyun Drive QR login result could not be decoded")

    async def poll(self, session_id: str) -> tuple[str, QrLoginSession]:
        session = self.get_session(session_id)
        if session.refresh_token:
            return "confirmed", session
        payload = await self._request(
            "/newlogin/qrcode/query.do",
            method="POST",
            params={"appName": "aliyun_drive", "fromSite": "52", "_bx-v": "2.0.31"},
            form={
                **self._form(),
                "t": session.t,
                "ck": session.ck,
                "navlanguage": "zh-CN",
                "navPlatform": "MacIntel",
            },
        )
        data = self._response_data(payload)
        upstream_status = str(data.get("qrCodeStatus", "NEW")).upper()
        statuses = {
            "NEW": "waiting",
            "SCANED": "scanned",
            "SCANNED": "scanned",
            "CONFIRMED": "confirmed",
            "EXPIRED": "expired",
        }
        status = statuses.get(upstream_status, "waiting")
        if status == "confirmed":
            session.refresh_token = self._decode_token(data)
        elif status == "expired":
            self._sessions.pop(session_id, None)
        return status, session

    def finish(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _discard_expired(self) -> None:
        now = datetime.now(UTC)
        for session_id, session in list(self._sessions.items()):
            if now >= session.expires_at:
                self._sessions.pop(session_id, None)
