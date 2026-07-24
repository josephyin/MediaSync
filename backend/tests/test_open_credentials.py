import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.security import get_credential_cipher
from app.models import Base, CloudAccount
from app.providers.base import AccountProfile, DriveRef
from app.schemas.cloud_account import OpenCredentialConfigure
from app.services.account_service import (
    DEFAULT_ALISTGO_TOKEN_URL,
    DEFAULT_OPENLIST_TOKEN_URL,
    configure_open_credential,
    get_open_provider,
    verify_open_credential,
)


class FakeAccountProvider:
    def __init__(self, user_id: str):
        self.user_id = user_id

    async def validate_account(self) -> AccountProfile:
        return AccountProfile(
            identity=self.user_id,
            user_id=self.user_id,
            default_drive_id="drive-1",
            drives=[DriveRef("drive-1", "resource", "资源库")],
        )

    def consume_refresh_token_update(self) -> None:
        return None


def make_account(db: Session) -> CloudAccount:
    account = CloudAccount(
        provider="aliyundrive",
        name="test",
        refresh_token=get_credential_cipher().encrypt("private-token"),
        status="active",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def test_configure_all_open_credential_modes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db)
        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(mode="alistgo", refresh_token="alist-token"),
        )
        assert account.open_auth_mode == "alistgo"
        assert account.open_token_url == DEFAULT_ALISTGO_TOKEN_URL
        assert account.open_refresh_token != "alist-token"
        assert get_open_provider(account).oauth_token_url == DEFAULT_ALISTGO_TOKEN_URL

        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(mode="openlist", refresh_token="openlist-token"),
        )
        assert account.open_auth_mode == "openlist"
        assert account.open_token_url == DEFAULT_OPENLIST_TOKEN_URL
        assert get_open_provider(account).oauth_token_url == DEFAULT_OPENLIST_TOKEN_URL

        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(
                mode="custom",
                refresh_token="custom-token",
                client_id="client-id",
                client_secret="client-secret",
            ),
        )
        provider = get_open_provider(account)
        assert account.open_auth_mode == "custom"
        assert account.open_token_url is None
        assert provider.client_id == "client-id"
        assert provider.client_secret == "client-secret"


async def test_open_credential_rejects_a_different_account(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db)
        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(mode="alistgo", refresh_token="alist-token"),
        )
        monkeypatch.setattr(
            "app.services.account_service.get_provider",
            lambda *_: FakeAccountProvider("private-user"),
        )
        monkeypatch.setattr(
            "app.services.account_service.get_open_provider",
            lambda *_: FakeAccountProvider("open-user"),
        )

        with pytest.raises(ValueError, match="different Aliyun Drive account"):
            await verify_open_credential(db, account)

        assert account.open_status == "error"
