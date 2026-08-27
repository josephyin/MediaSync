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
    DEFAULT_QUARK_OPENLIST_TOKEN_URL,
    configure_open_credential,
    get_open_provider,
    get_runtime_provider,
    persist_provider_token,
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


def make_account(db: Session, provider: str = "aliyundrive") -> CloudAccount:
    account = CloudAccount(
        provider=provider,
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


def test_configure_quark_openlist_credential_reuses_encrypted_open_fields() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db, "quark")
        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(
                mode="openlist",
                refresh_token="quark-refresh-token",
                client_id="quark-app-id",
                client_secret="quark-sign-key",
            ),
        )

        provider = get_open_provider(account)
        assert account.open_auth_mode == "openlist"
        assert account.open_token_url == DEFAULT_QUARK_OPENLIST_TOKEN_URL
        assert account.open_client_id == "quark-app-id"
        assert account.open_client_secret != "quark-sign-key"
        assert provider.refresh_token == "quark-refresh-token"
        assert provider.app_id == "quark-app-id"
        assert provider.sign_key == "quark-sign-key"


def test_quark_openlist_rejects_missing_app_credentials() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db, "quark")
        with pytest.raises(ValueError, match="AppID and SignKey are required"):
            configure_open_credential(
                db,
                account,
                OpenCredentialConfigure(mode="openlist", refresh_token="token"),
            )


def test_configure_baidu_openlist_credential_requires_no_app_keys() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db, "baidu")
        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(
                mode="openlist",
                refresh_token="baidu-refresh-token",
                token_url="https://api.oplist.org/baiduyun/renewapi",
            ),
        )

        provider = get_open_provider(account)

        assert account.open_auth_mode == "openlist"
        assert account.open_client_id is None
        assert account.open_client_secret is None
        assert provider.refresh_token == "baidu-refresh-token"


def test_baidu_runtime_provider_combines_credentials_and_persists_open_rotation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db, "baidu")
        account.refresh_token = get_credential_cipher().encrypt("BDUSS=session-value")
        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(
                mode="openlist",
                refresh_token="baidu-refresh-token",
            ),
        )
        account.open_status = "active"
        db.commit()

        provider = get_runtime_provider(account)
        provider._open._refresh_token_update = "baidu-rotated-token"

        assert persist_provider_token(account, provider) is True
        assert (
            get_credential_cipher().decrypt(account.open_refresh_token)
            == "baidu-rotated-token"
        )


@pytest.mark.parametrize("mode", ["alistgo", "custom"])
def test_quark_rejects_non_openlist_modes(mode: str) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db, "quark")
        with pytest.raises(ValueError, match="requires OpenList mode"):
            configure_open_credential(
                db,
                account,
                OpenCredentialConfigure(
                    mode=mode,
                    refresh_token="token",
                    client_id="app",
                    client_secret="secret",
                ),
            )


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

        with pytest.raises(ValueError, match="different cloud account"):
            await verify_open_credential(db, account)

        assert account.open_status == "error"


async def test_quark_open_credential_allows_independent_validation_without_private_user_id(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = make_account(db, "quark")
        configure_open_credential(
            db,
            account,
            OpenCredentialConfigure(
                mode="openlist",
                refresh_token="token",
                client_id="app",
                client_secret="secret",
            ),
        )
        monkeypatch.setattr(
            "app.services.account_service.get_provider",
            lambda *_: FakeAccountProvider(""),
        )
        monkeypatch.setattr(
            "app.services.account_service.get_open_provider",
            lambda *_: FakeAccountProvider("open-user"),
        )

        verified = await verify_open_credential(db, account)

        assert verified.open_status == "active"
        assert verified.open_account_identity == "open-user"
