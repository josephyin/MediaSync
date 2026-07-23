import base64
import hashlib
import secrets
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings, get_settings

SESSION_COOKIE = "mediasync_session"


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="mediasync-admin-session")


def authenticate_admin(username: str, password: str, settings: Settings) -> bool:
    return secrets.compare_digest(username, settings.admin_username) and secrets.compare_digest(
        password, settings.admin_password
    )


def create_session(response: Response, username: str, settings: Settings) -> None:
    value = _serializer(settings).dumps({"sub": username})
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def require_admin(request: Request) -> str:
    settings = get_settings()
    value = request.cookies.get(SESSION_COOKIE)
    if not value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload: dict[str, Any] = _serializer(settings).loads(
            value, max_age=settings.session_max_age_seconds
        )
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired"
        ) from exc
    username = str(payload.get("sub", ""))
    if not secrets.compare_digest(username, settings.admin_username):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")
    return username


class CredentialCipher:
    def __init__(self, key_material: str):
        digest = hashlib.sha256(key_material.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Unable to decrypt credential") from exc


def get_credential_cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().credential_encryption_key)
