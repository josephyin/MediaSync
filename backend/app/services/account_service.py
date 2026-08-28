from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import get_credential_cipher
from app.models import CloudAccount
from app.providers import get_provider
from app.providers.aliyundrive.provider import AliyunDriveProvider
from app.providers.baidu.open_provider import (
    DEFAULT_ALISTGO_CLIENT_ID as DEFAULT_BAIDU_ALISTGO_CLIENT_ID,
)
from app.providers.baidu.open_provider import (
    DEFAULT_ALISTGO_CLIENT_SECRET as DEFAULT_BAIDU_ALISTGO_CLIENT_SECRET,
)
from app.providers.baidu.open_provider import (
    DEFAULT_OPENLIST_TOKEN_URL as DEFAULT_BAIDU_OPENLIST_TOKEN_URL,
)
from app.providers.baidu.open_provider import BaiduOpenProvider
from app.providers.baidu.provider import BaiduProvider
from app.providers.base import CloudDriveProvider
from app.providers.pan123.open_provider import Pan123OpenProvider
from app.providers.quark.open_provider import (
    DEFAULT_OPENLIST_TOKEN_URL as DEFAULT_QUARK_OPENLIST_TOKEN_URL,
)
from app.providers.quark.open_provider import QuarkOpenProvider
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountUpdate,
    OpenCredentialConfigure,
)

DEFAULT_ALISTGO_TOKEN_URL = "https://api.alistgo.com/alist/ali_open/token"
DEFAULT_OPENLIST_TOKEN_URL = "https://api.oplist.org.cn/alicloud/renewapi"
HOSTED_OPEN_AUTH_MODES = {"alistgo", "openlist"}
OPEN_CREDENTIAL_PROVIDERS = {"aliyundrive", "quark", "pan123", "baidu"}


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
    changed = False
    if rotated_token:
        account.refresh_token = get_credential_cipher().encrypt(rotated_token)
        changed = True
    open_consumer = getattr(provider, "consume_open_refresh_token_update", None)
    if callable(open_consumer):
        rotated_open_token = open_consumer()
        if rotated_open_token:
            account.open_refresh_token = get_credential_cipher().encrypt(rotated_open_token)
            changed = True
    return changed


def configure_open_credential(
    db: Session, account: CloudAccount, payload: OpenCredentialConfigure
) -> CloudAccount:
    if account.provider not in OPEN_CREDENTIAL_PROVIDERS:
        raise ValueError("OpenAPI binding is not available for this provider")
    if account.provider == "quark" and payload.mode != "openlist":
        raise ValueError("quark OpenAPI currently requires OpenList mode")
    if account.provider == "pan123" and payload.mode != "custom":
        raise ValueError(
            "123 OpenAPI only supports a self-owned application; public hosted login is unavailable"
        )
    cipher = get_credential_cipher()
    mode_changed = account.open_auth_mode not in (None, payload.mode)
    refresh_token = payload.refresh_token.strip() if payload.refresh_token else None
    refresh_required = not (account.provider == "pan123" and payload.mode == "custom")
    if refresh_token:
        account.open_refresh_token = cipher.encrypt(refresh_token)
    elif refresh_required and (not account.open_refresh_token or mode_changed):
        raise ValueError("OpenAPI refresh token is required")
    elif not refresh_required and mode_changed:
        account.open_refresh_token = None

    if account.provider == "baidu" and payload.mode == "alistgo":
        account.open_token_url = None
        account.open_client_id = DEFAULT_BAIDU_ALISTGO_CLIENT_ID
        account.open_client_secret = cipher.encrypt(DEFAULT_BAIDU_ALISTGO_CLIENT_SECRET)
    elif payload.mode in HOSTED_OPEN_AUTH_MODES:
        if account.provider == "quark":
            default_url = DEFAULT_QUARK_OPENLIST_TOKEN_URL
        elif account.provider == "baidu":
            default_url = DEFAULT_BAIDU_OPENLIST_TOKEN_URL
        else:
            default_url = (
                DEFAULT_ALISTGO_TOKEN_URL
                if payload.mode == "alistgo"
                else DEFAULT_OPENLIST_TOKEN_URL
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
        if account.provider == "quark":
            app_id = payload.client_id.strip() if payload.client_id else None
            sign_key = payload.client_secret.strip() if payload.client_secret else None
            effective_app_id = app_id or (None if mode_changed else account.open_client_id)
            effective_sign_key = bool(sign_key or (not mode_changed and account.open_client_secret))
            if not effective_app_id or not effective_sign_key:
                raise ValueError("Quark OpenAPI AppID and SignKey are required")
            account.open_client_id = effective_app_id
            if sign_key:
                account.open_client_secret = cipher.encrypt(sign_key)
        else:
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


def get_open_provider(account: CloudAccount, drive_id: str | None = None) -> CloudDriveProvider:
    if not account.open_auth_mode:
        raise ValueError("OpenAPI is not bound")
    cipher = get_credential_cipher()
    client_secret = cipher.decrypt(account.open_client_secret) if account.open_client_secret else ""
    refresh_token = cipher.decrypt(account.open_refresh_token) if account.open_refresh_token else ""
    if account.provider == "aliyundrive":
        return AliyunDriveProvider(
            refresh_token=refresh_token,
            client_id=account.open_client_id or "",
            client_secret=client_secret,
            api_base_url=get_settings().aliyundrive_api_base_url,
            oauth_token_url=(
                account.open_token_url if account.open_auth_mode in HOSTED_OPEN_AUTH_MODES else None
            ),
            drive_id=drive_id,
        )
    if account.provider == "quark":
        if account.open_auth_mode != "openlist" or not account.open_token_url:
            raise ValueError("Quark OpenAPI requires OpenList mode")
        if drive_id not in (None, "0"):
            raise ValueError("Quark OpenAPI currently exposes only its default drive")
        return QuarkOpenProvider(
            refresh_token=refresh_token,
            app_id=account.open_client_id or "",
            sign_key=client_secret,
            oauth_token_url=account.open_token_url,
        )
    if account.provider == "baidu":
        if drive_id not in (None, "root", "/"):
            raise ValueError("Baidu OpenAPI currently exposes only its default drive")
        return BaiduOpenProvider(
            refresh_token=refresh_token,
            oauth_token_url=(
                account.open_token_url if account.open_auth_mode == "openlist" else None
            ),
            client_id=account.open_client_id or "",
            client_secret=client_secret,
        )
    if account.provider == "pan123":
        if account.open_auth_mode != "custom":
            raise ValueError("123 OpenAPI requires a self-owned application")
        if drive_id not in (None, "0", "root"):
            raise ValueError("123 OpenAPI currently exposes only its default drive")
        return Pan123OpenProvider(
            client_id=account.open_client_id or "",
            client_secret=client_secret,
        )
    raise ValueError("OpenAPI binding is not available for this provider")


def get_runtime_provider(account: CloudAccount, drive_id: str | None = None) -> CloudDriveProvider:
    private_token = get_decrypted_token(account)
    if account.provider != "baidu":
        return get_provider(account.provider, private_token, drive_id)
    if account.open_status != "active" or not account.open_auth_mode:
        raise ValueError("Baidu transfers require a verified OpenAPI credential")
    open_provider = get_open_provider(account, drive_id)
    if not isinstance(open_provider, BaiduOpenProvider):
        raise ValueError("Baidu OpenAPI provider is unavailable")
    return BaiduProvider(private_token, open_provider)


def persist_open_provider_token(account: CloudAccount, provider: CloudDriveProvider) -> bool:
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
        if account.provider == "aliyundrive" and (
            not account.provider_user_id or not profile.user_id
        ):
            raise ValueError("Aliyun Drive did not return user IDs for account matching")
        if (
            account.provider_user_id
            and profile.user_id
            and account.provider_user_id != profile.user_id
        ):
            raise ValueError("OpenAPI token belongs to a different cloud account")
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
