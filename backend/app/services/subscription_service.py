from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.security import get_credential_cipher
from app.models import CloudAccount, Subscription
from app.providers import get_provider
from app.scheduler.schedule import parse_schedule
from app.schemas.subscription import SubscriptionCreate, SubscriptionUpdate
from app.services.account_service import get_decrypted_token


async def create_subscription(
    db: Session, account: CloudAccount, payload: SubscriptionCreate
) -> Subscription:
    if account.provider != payload.provider:
        raise ValueError("Subscription provider does not match cloud account")
    provider = get_provider(account.provider, get_decrypted_token(account))
    share = await provider.resolve_share(payload.share_url, payload.share_password)
    encrypted_password = (
        get_credential_cipher().encrypt(payload.share_password) if payload.share_password else None
    )
    interval = parse_schedule(payload.schedule)
    subscription = Subscription(
        cloud_account_id=account.id,
        name=payload.name,
        provider=payload.provider,
        share_url=payload.share_url,
        share_key=share.share_key,
        share_password=encrypted_password,
        source_folder_id=payload.source_folder_id or share.root_folder_id,
        target_path=payload.target_path,
        target_drive_id=payload.target_drive_id or account.default_drive_id,
        target_drive_type=payload.target_drive_type or "default",
        schedule=payload.schedule,
        enabled=payload.enabled,
        status="active" if payload.enabled else "disabled",
        initial_sync_mode=payload.initial_sync_mode,
        next_scan_at=datetime.now(UTC) + interval.delta if payload.enabled else None,
    )
    db.add(subscription)
    db.commit()
    db.refresh(subscription)
    return subscription


def update_subscription(
    db: Session, subscription: Subscription, payload: SubscriptionUpdate
) -> Subscription:
    values = payload.model_dump(exclude_unset=True)
    for key, value in values.items():
        setattr(subscription, key, value)
    if payload.enabled is not None:
        subscription.status = "active" if payload.enabled else "disabled"
    if subscription.enabled:
        interval = parse_schedule(subscription.schedule)
        subscription.next_scan_at = datetime.now(UTC) + interval.delta
    else:
        subscription.next_scan_at = None
    db.commit()
    db.refresh(subscription)
    return subscription


def decrypt_share_password(subscription: Subscription) -> str | None:
    if not subscription.share_password:
        return None
    return get_credential_cipher().decrypt(subscription.share_password)
