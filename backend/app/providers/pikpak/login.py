"""One-shot login; never persist the supplied password or retry sign-in."""

import json
import re
from uuid import uuid4

import httpx

from app.core.exceptions import ProviderRequestError
from app.providers.pikpak.provider import AUTH_URL, WEB_CLIENT_ID, PikPakProvider

# Public Web client identifier/secret, not a user credential.
WEB_CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Coyg"


async def login_with_password(
    username: str,
    password: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    username = username.strip()
    if not username or not password or len(password) > 1024:
        raise ProviderRequestError("请填写 PikPak 账号和密码")
    device_id = uuid4().hex
    # Reuse the fixed-host transport and sanitized errors, without authenticating
    # with the placeholder token. The password exists only in this request.
    transport = PikPakProvider("unused", http_client=http_client)
    identity_type = (
        "email"
        if "@" in username
        else ("phone_number" if re.fullmatch(r"\+?\d{11,18}", username) else "username")
    )
    captcha = await transport._send(
        "POST",
        f"{AUTH_URL}/v1/shield/captcha/init",
        json={
            "client_id": WEB_CLIENT_ID,
            "device_id": device_id,
            "action": f"POST:{AUTH_URL}/v1/auth/signin",
            "meta": {identity_type: username},
        },
    )
    token = captcha.get("captcha_token")
    if captcha.get("url") or not isinstance(token, str) or not token:
        raise ProviderRequestError(
            "PikPak 要求人机验证，当前无法自动登录。请在官方网页完成验证后重试；"
            "如仍受限，可使用已有 Refresh Token 添加账号。"
        )
    result = await transport._send(
        "POST",
        f"{AUTH_URL}/v1/auth/signin",
        headers={"X-Device-Id": device_id},
        json={
            "client_id": WEB_CLIENT_ID,
            "client_secret": WEB_CLIENT_SECRET,
            "username": username,
            "password": password,
            "captcha_token": token,
        },
    )
    refresh, user_id = result.get("refresh_token"), result.get("sub")
    if not isinstance(refresh, str) or not refresh or not isinstance(user_id, str) or not user_id:
        raise ProviderRequestError("PikPak 登录响应不完整，未保存账号，请重试")
    return json.dumps(
        {"refresh_token": refresh, "client_id": WEB_CLIENT_ID, "device_id": device_id},
        separators=(",", ":"),
    ), user_id
