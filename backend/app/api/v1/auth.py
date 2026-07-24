from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.config import get_settings
from app.core.security import (
    SESSION_COOKIE,
    authenticate_admin,
    clear_session,
    create_session,
    require_admin,
)
from app.schemas.auth import AuthStatus, LoginRequest
from app.schemas.common import MessageResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=AuthStatus)
def login(payload: LoginRequest, response: Response) -> AuthStatus:
    settings = get_settings()
    if not authenticate_admin(payload.username, payload.password, settings):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    create_session(response, payload.username, settings)
    return AuthStatus(authenticated=True, username=payload.username)


@router.post("/logout", response_model=MessageResponse)
def logout(response: Response) -> MessageResponse:
    clear_session(response)
    return MessageResponse(message="Logged out")


@router.get("/status", response_model=AuthStatus)
def auth_status(request: Request) -> AuthStatus:
    if not request.cookies.get(SESSION_COOKIE):
        return AuthStatus(authenticated=False)
    try:
        username = require_admin(request)
    except HTTPException:
        return AuthStatus(authenticated=False)
    return AuthStatus(authenticated=True, username=username)
