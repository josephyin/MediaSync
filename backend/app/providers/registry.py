from collections.abc import Callable

from app.core.config import get_settings
from app.core.exceptions import ProviderError
from app.providers.aliyundrive.private_provider import AliyunDrivePrivateProvider
from app.providers.aliyundrive.provider import AliyunDriveProvider
from app.providers.base import CloudDriveProvider
from app.providers.pan123.provider import Pan123PrivateProvider
from app.providers.quark.provider import QuarkPrivateProvider

ProviderFactory = Callable[[str, str | None], CloudDriveProvider]


def _aliyundrive_factory(refresh_token: str, drive_id: str | None = None) -> CloudDriveProvider:
    settings = get_settings()
    if settings.aliyundrive_mode == "private_api":
        return AliyunDrivePrivateProvider(
            refresh_token=refresh_token,
            api_base_url=settings.aliyundrive_private_api_base_url,
            auth_base_url=settings.aliyundrive_private_auth_base_url,
            drive_id=drive_id,
        )
    if settings.aliyundrive_mode != "official":
        raise ProviderError("Invalid ALIYUNDRIVE_MODE; expected 'private_api' or 'official'")
    return AliyunDriveProvider(
        refresh_token=refresh_token,
        client_id=settings.aliyundrive_client_id,
        client_secret=settings.aliyundrive_client_secret,
        api_base_url=settings.aliyundrive_api_base_url,
        drive_id=drive_id,
    )


def _quark_factory(cookie: str, drive_id: str | None = None) -> CloudDriveProvider:
    if drive_id not in (None, "0"):
        raise ProviderError("Quark Drive currently exposes only its default drive")
    return QuarkPrivateProvider(cookie=cookie)


def _pan123_factory(access_token: str, drive_id: str | None = None) -> CloudDriveProvider:
    if drive_id not in (None, "0"):
        raise ProviderError("123 Cloud Drive currently exposes only its default drive")
    return Pan123PrivateProvider(access_token=access_token)


PROVIDERS: dict[str, ProviderFactory] = {
    "aliyundrive": _aliyundrive_factory,
    "quark": _quark_factory,
    "pan123": _pan123_factory,
}


def get_provider(
    provider: str, refresh_token: str, drive_id: str | None = None
) -> CloudDriveProvider:
    factory = PROVIDERS.get(provider)
    if factory is None:
        raise ProviderError(f"Unsupported provider: {provider}")
    return factory(refresh_token, drive_id)


def list_provider_types() -> list[dict[str, object]]:
    mode = get_settings().aliyundrive_mode
    private_mode = mode == "private_api"
    return [
        {
            "id": "aliyundrive",
            "name": "Aliyun Drive",
            "enabled": True,
            "status": "experimental" if private_mode else "partial",
            "mode": mode,
            "capabilities": (
                [
                    "account_verify",
                    "share_browse",
                    "folder_browse",
                    "folder_create",
                    "share_save",
                ]
                if private_mode
                else ["account_verify", "folder_browse", "folder_create"]
            ),
        },
        {
            "id": "quark",
            "name": "Quark Drive",
            "enabled": True,
            "status": "experimental",
            "mode": "private_api",
            "capabilities": [
                "account_verify",
                "share_browse",
                "folder_browse",
                "folder_create",
                "share_save",
            ],
        },
        {
            "id": "pan123",
            "name": "123 Cloud Drive",
            "enabled": True,
            "status": "experimental",
            "mode": "private_api",
            "capabilities": [
                "account_verify",
                "share_browse",
                "folder_browse",
                "folder_create",
                "share_save",
            ],
        },
        {"id": "115", "name": "115", "enabled": False, "capabilities": []},
        {"id": "onedrive", "name": "OneDrive", "enabled": False, "capabilities": []},
    ]
