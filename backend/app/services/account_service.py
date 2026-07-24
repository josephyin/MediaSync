from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import get_credential_cipher
from app.models import CloudAccount
from app.providers import get_provider
from app.providers.aliyundrive.provider import AliyunDriveProvider
from app.providers.base import CloudDriveProvider
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountUpdate,
    OpenCredentialConfigure,
)

DEFAULT_ALISTGO_TOKEN_URL = "https://api.alistgo.com/alist/ali_open/token"
DEFAULT_OPENLIST_TOKEN_URL = "https://api.oplist.org.cn/alicloud/renewapi"
HOSTED_OPEN_AUTH_MODES = {"alistgo", "openlist"}


def create_account(db: Session, payload: CloudAccountCreate) -> CloudAccount:
    refresh_token = payload.refresh_token.strip()
    if not refresh_token:
        raise ValueError("Refresh token cannot be empty")
    account = CloudAccount(
        provider=payload.provider,
        name=payload.name,
        refresh_token=get_credential_cipher().encrypt(refresh_token),
        status="active",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def update_account(db: Session, account: CloudAccount, payload: CloudAccountUpdate) -> CloudAccount:
    values = payload.model_dump(exclude_unset=True)
    refresh_token = values.pop("refresh_token", None)
    for key, value in values.items():
        setattr(account, key, value)
    if refresh_token:
        account.refresh_token = get_credential_cipher().encrypt(refresh_token.strip())
        account.status = "active"
        account.last_error = None
    db.commit()
    db.refresh(account)
    return account


def get_decrypted_token(account: CloudAccount) -> str:
    return get_credential_cipher().decrypt(account.refresh_token)


async def verify_account(db: Session, account: CloudAccount) -> CloudAccount:
    from app.models.base import utcnow

    provider = get_provider(account.provider, get_decrypted_token(account))
    error: Exception | None = None
    try:
        profile = await provider.validate_account()
        account.account_identity = profile.identity
        account.provider_user_id = profile.user_id
        account.default_drive_id = profile.default_drive_id
        account.status = "active"
        account.last_error = None
        account.last_verified_at = utcnow()
    except Exception as exc:
        account.status = "error"
        account.last_error = str(exc)
        error = exc
    persist_provider_token(account, provider)
    db.commit()
    db.refresh(account)
    if error is not None:
        raise error
    return account


def persist_provider_token(account: CloudAccount, provider: CloudDriveProvider) -> bool:
    rotated_token = provider.consume_refresh_token_update()
    if not rotated_token:
        return False
    account.refresh_token = get_credential_cipher().encrypt(rotated_token)
    return True


def configure_open_credential(
    db: Session, account: CloudAccount, payload: OpenCredentialConfigure
) -> CloudAccount:
    if account.provider != "aliyundrive":
        raise ValueError("OpenAPI binding is only available for Aliyun Drive")
    cipher = get_credential_cipher()
    mode_changed = account.open_auth_mode not in (None, payload.mode)
    refresh_token = payload.refresh_token.strip() if payload.refresh_token else None
    if refresh_token:
        account.open_refresh_token = cipher.encrypt(refresh_token)
    elif not account.open_refresh_token or mode_changed:
        raise ValueError("OpenAPI refresh token is required")

    if payload.mode in HOSTED_OPEN_AUTH_MODES:
        default_url = (
            DEFAULT_ALISTGO_TOKEN_URL if payload.mode == "alistgo" else DEFAULT_OPENLIST_TOKEN_URL
        )
        token_url = (payload.token_url or default_url).strip()
        parsed_token_url = urlparse(token_url)
        if parsed_token_url.scheme != "https" or not parsed_token_url.hostname:
            raise ValueError("Hosted OAuth token URL must be a valid HTTPS URL")
        if (
            parsed_token_url.username
            or parsed_token_url.password
            or parsed_token_url.query
            or parsed_token_url.fragment
        ):
            raise ValueError(
                "Hosted OAuth token URL cannot contain credentials, query, or fragment"
            )
        account.open_token_url = token_url
        account.open_client_id = None
        account.open_client_secret = None
    else:
        client_id = payload.client_id.strip() if payload.client_id else None
        client_secret = payload.client_secret.strip() if payload.client_secret else None
        if client_id:
            account.open_client_id = client_id
        elif not account.open_client_id or mode_changed:
            raise ValueError("OpenAPI Client ID is required")
        if client_secret:
            account.open_client_secret = cipher.encrypt(client_secret)
        elif not account.open_client_secret or mode_changed:
            raise ValueError("OpenAPI Client Secret is required")
        account.open_token_url = None

    account.open_auth_mode = payload.mode
    account.open_status = "pending"
    account.open_last_error = None
    db.commit()
    db.refresh(account)
    return account


def get_open_provider(account: CloudAccount) -> AliyunDriveProvider:
    if not account.open_auth_mode or not account.open_refresh_token:
        raise ValueError("Aliyun Drive OpenAPI is not bound")
    cipher = get_credential_cipher()
    client_secret = cipher.decrypt(account.open_client_secret) if account.open_client_secret else ""
    return AliyunDriveProvider(
        refresh_token=cipher.decrypt(account.open_refresh_token),
        client_id=account.open_client_id or "",
        client_secret=client_secret,
        api_base_url=get_settings().aliyundrive_api_base_url,
        oauth_token_url=(
            account.open_token_url if account.open_auth_mode in HOSTED_OPEN_AUTH_MODES else None
        ),
    )


def persist_open_provider_token(account: CloudAccount, provider: AliyunDriveProvider) -> bool:
    rotated_token = provider.consume_refresh_token_update()
    if not rotated_token:
        return False
    account.open_refresh_token = get_credential_cipher().encrypt(rotated_token)
    return True


async def verify_open_credential(db: Session, account: CloudAccount) -> CloudAccount:
    from app.models.base import utcnow

    # Refresh private account metadata first so the user ID comparison is based
    # on current credentials rather than a display name.
    await verify_account(db, account)
    provider = get_open_provider(account)
    error: Exception | None = None
    try:
        profile = await provider.validate_account()
        if not account.provider_user_id or not profile.user_id:
            raise ValueError("Aliyun Drive did not return user IDs for account matching")
        if account.provider_user_id != profile.user_id:
            raise ValueError("OpenAPI token belongs to a different Aliyun Drive account")
        account.open_account_identity = profile.identity
        account.open_status = "active"
        account.open_last_verified_at = utcnow()
        account.open_last_error = None
    except Exception as exc:
        account.open_status = "error"
        account.open_last_error = str(exc)
        error = exc
    persist_open_provider_token(account, provider)
    db.commit()
    db.refresh(account)
    if error is not None:
        raise error
    return account


def remove_open_credential(db: Session, account: CloudAccount) -> CloudAccount:
    account.open_auth_mode = None
    account.open_refresh_token = None
    account.open_client_id = None
    account.open_client_secret = None
    account.open_token_url = None
    account.open_account_identity = None
    account.open_status = None
    account.open_last_verified_at = None
    account.open_last_error = None
    db.commit()
    db.refresh(account)
    return account


def account_has_subscriptions(db: Session, account_id: int) -> bool:
    from app.models import Subscription

    return (
        db.scalar(select(Subscription.id).where(Subscription.cloud_account_id == account_id))
        is not None
    )
