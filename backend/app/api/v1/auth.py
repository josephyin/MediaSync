from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.admin_credentials import (
    AdminCredentialError,
    AdminPasswordPersistenceError,
    InvalidCurrentPasswordError,
    get_admin_credential_store,
)
from app.core.config import get_settings
from app.core.security import (
    SESSION_COOKIE,
    authenticate_admin,
    clear_session,
    create_session,
    require_admin,
)
from app.schemas.auth import AuthStatus, LoginRequest, PasswordChangeRequest
from app.schemas.common import MessageResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=AuthStatus)
def login(payload: LoginRequest, response: Response) -> AuthStatus:
    settings = get_settings()
    credential_store = get_admin_credential_store()
    if not authenticate_admin(payload.username, payload.password, settings):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    create_session(response, payload.username, settings)
    return AuthStatus(
        authenticated=True,
        username=payload.username,
        password_change_supported=credential_store.password_change_supported,
    )


@router.post("/logout", response_model=MessageResponse)
def logout(response: Response) -> MessageResponse:
    clear_session(response)
    return MessageResponse(message="Logged out")


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request) -> AuthStatus:
    credential_store = get_admin_credential_store()
    if not request.cookies.get(SESSION_COOKIE):
        return AuthStatus(
            authenticated=False,
            password_change_supported=credential_store.password_change_supported,
        )
    try:
        username = require_admin(request)
    except HTTPException:
        return AuthStatus(
            authenticated=False,
            password_change_supported=credential_store.password_change_supported,
        )
    return AuthStatus(
        authenticated=True,
        username=username,
        password_change_supported=credential_store.password_change_supported,
    )


@router.post("/password", response_model=MessageResponse)
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
) -> MessageResponse:
    require_admin(request)
    credential_store = get_admin_credential_store()
    if not credential_store.password_change_supported:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "当前部署模式不支持在线修改密码，请修改 .env 中的 "
                "ADMIN_PASSWORD 并重建 API 容器"
            ),
        )
    try:
        credential_store.change_password(
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
    except InvalidCurrentPasswordError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="当前密码或新密码不符合要求",
        ) from exc
    except AdminPasswordPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="密码保存失败，原密码仍然有效",
        ) from exc
    except AdminCredentialError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    clear_session(response)
    return MessageResponse(message="密码已修改，请重新登录")
