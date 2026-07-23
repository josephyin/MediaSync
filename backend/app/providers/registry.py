from collections.abc import Callable

from app.core.config import get_settings
from app.core.exceptions import ProviderError
from app.providers.aliyundrive.private_provider import AliyunDrivePrivateProvider
from app.providers.aliyundrive.provider import AliyunDriveProvider
from app.providers.base import CloudDriveProvider

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


PROVIDERS: dict[str, ProviderFactory] = {"aliyundrive": _aliyundrive_factory}


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
        {"id": "quark", "name": "Quark Drive", "enabled": False, "capabilities": []},
        {"id": "115", "name": "115", "enabled": False, "capabilities": []},
        {"id": "onedrive", "name": "OneDrive", "enabled": False, "capabilities": []},
    ]
