from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.core.exceptions import ProviderRequestError
from app.models import CloudAccount
from app.models.base import utcnow
from app.providers import get_provider, list_provider_types
from app.providers.aliyundrive.qr_login import AliyunDriveQrLogin
from app.providers.baidu.qr_login import BaiduQrLogin
from app.providers.pan123.qr_login import Pan123QrLogin
from app.schemas.cloud_account import (
    CloudAccountCreate,
    CloudAccountRead,
    CloudAccountUpdate,
    DriveInfo,
    FolderItem,
    OpenCredentialConfigure,
    QrLoginStartRequest,
    QrLoginStartResponse,
    QrLoginStatusResponse,
)
from app.schemas.common import MessageResponse, Page
from app.services.account_service import (
    account_has_subscriptions,
    configure_open_credential,
    create_account,
    get_decrypted_token,
    get_open_provider,
    persist_open_provider_token,
    persist_provider_token,
    remove_open_credential,
    update_account,
    verify_account,
    verify_open_credential,
)

router = APIRouter(tags=["cloud-accounts"])
qr_login = AliyunDriveQrLogin(get_settings().aliyundrive_qr_login_base_url)
pan123_qr_login = Pan123QrLogin()
baidu_qr_login = BaiduQrLogin()


def _get_account(db: DbSession, account_id: int) -> CloudAccount:
    account = db.get(CloudAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Cloud account not found")
    return account


@router.get("/provider-types")
def provider_types(_: AdminUser) -> list[dict[str, object]]:
    return list_provider_types()


@router.get("/cloud-accounts", response_model=Page[CloudAccountRead])
def list_accounts(
    db: DbSession, _: AdminUser, page: int = 1, page_size: int = 20
) -> Page[CloudAccountRead]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    total = db.scalar(select(func.count()).select_from(CloudAccount)) or 0
    items = list(
        db.scalars(
            select(CloudAccount)
            .order_by(CloudAccount.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return Page(items=items, page=page, page_size=page_size, total=total)


@router.post("/cloud-accounts", response_model=CloudAccountRead, status_code=201)
def add_account(payload: CloudAccountCreate, db: DbSession, _: AdminUser) -> CloudAccount:
    try:
        return create_account(db, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Account name already exists") from exc


@router.get("/cloud-accounts/{account_id}", response_model=CloudAccountRead)
def get_account(account_id: int, db: DbSession, _: AdminUser) -> CloudAccount:
    return _get_account(db, account_id)


@router.patch("/cloud-accounts/{account_id}", response_model=CloudAccountRead)
def patch_account(
    account_id: int, payload: CloudAccountUpdate, db: DbSession, _: AdminUser
) -> CloudAccount:
    account = _get_account(db, account_id)
    try:
        return update_account(db, account, payload)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Account name already exists") from exc


@router.delete("/cloud-accounts/{account_id}", response_model=MessageResponse)
def delete_account(account_id: int, db: DbSession, _: AdminUser) -> MessageResponse:
    account = _get_account(db, account_id)
    if account_has_subscriptions(db, account.id):
        raise HTTPException(
            status_code=409,
            detail="请先删除使用该账号的分享订阅，再删除云盘账号",
        )
    db.delete(account)
    db.commit()
    return MessageResponse(message="Cloud account deleted")


@router.post("/aliyundrive/qr-login/start", response_model=QrLoginStartResponse)
async def start_aliyundrive_qr_login(
    payload: QrLoginStartRequest, db: DbSession, _: AdminUser
) -> QrLoginStartResponse:
    if get_settings().aliyundrive_mode != "private_api":
        raise HTTPException(status_code=409, detail="扫码登录仅适用于 private_api 模式")
    account = _get_account(db, payload.account_id) if payload.account_id else None
    if account and account.provider != "aliyundrive":
        raise HTTPException(status_code=422, detail="账号不是 Aliyun Drive Provider")
    name = account.name if account else (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="新增账号时必须填写账号名称")
    try:
        result = await qr_login.start(
            account_id=account.id if account else None,
            account_name=name,
        )
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return QrLoginStartResponse(
        session_id=result.session_id,
        qr_code_data_url=result.qr_code_data_url,
        expires_in=result.expires_in,
    )


@router.get("/aliyundrive/qr-login/{session_id}", response_model=QrLoginStatusResponse)
async def poll_aliyundrive_qr_login(
    session_id: str, db: DbSession, _: AdminUser
) -> QrLoginStatusResponse:
    try:
        login_status, session = await qr_login.poll(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if login_status != "confirmed" or not session.refresh_token:
        return QrLoginStatusResponse(status=login_status)

    try:
        if session.account_id:
            account = _get_account(db, session.account_id)
            account = update_account(
                db,
                account,
                CloudAccountUpdate(refresh_token=session.refresh_token),
            )
        else:
            account = create_account(
                db,
                CloudAccountCreate(
                    provider="aliyundrive",
                    name=session.account_name or "Aliyun Drive",
                    refresh_token=session.refresh_token,
                ),
            )
        account = await verify_account(db, account)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="账号名称已经存在") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    qr_login.finish(session_id)
    return QrLoginStatusResponse(status="confirmed", account=account)


@router.post("/pan123/qr-login/start", response_model=QrLoginStartResponse)
async def start_pan123_qr_login(
    payload: QrLoginStartRequest, db: DbSession, _: AdminUser
) -> QrLoginStartResponse:
    account = _get_account(db, payload.account_id) if payload.account_id else None
    if account and account.provider != "pan123":
        raise HTTPException(status_code=422, detail="账号不是 123 Cloud Drive Provider")
    name = account.name if account else (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="新增账号时必须填写账号名称")
    try:
        result = await pan123_qr_login.start(
            account_id=account.id if account else None,
            account_name=name,
        )
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return QrLoginStartResponse(
        session_id=result.session_id,
        qr_code_data_url=result.qr_code_data_url,
        expires_in=result.expires_in,
    )


@router.get("/pan123/qr-login/{session_id}", response_model=QrLoginStatusResponse)
async def poll_pan123_qr_login(
    session_id: str, db: DbSession, _: AdminUser
) -> QrLoginStatusResponse:
    try:
        login_status, session = await pan123_qr_login.poll(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if login_status != "confirmed" or not session.access_token:
        return QrLoginStatusResponse(status=login_status)

    try:
        if session.account_id:
            account = _get_account(db, session.account_id)
            account = update_account(
                db,
                account,
                CloudAccountUpdate(refresh_token=session.access_token),
            )
        else:
            account = create_account(
                db,
                CloudAccountCreate(
                    provider="pan123",
                    name=session.account_name or "123 Cloud Drive",
                    refresh_token=session.access_token,
                ),
            )
        account = await verify_account(db, account)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="账号名称已经存在") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await pan123_qr_login.finish(session_id)
    return QrLoginStatusResponse(status="confirmed", account=account)


@router.post("/baidu/qr-login/start", response_model=QrLoginStartResponse)
async def start_baidu_qr_login(
    payload: QrLoginStartRequest, db: DbSession, _: AdminUser
) -> QrLoginStartResponse:
    account = _get_account(db, payload.account_id) if payload.account_id else None
    if account and account.provider != "baidu":
        raise HTTPException(status_code=422, detail="账号不是 Baidu Netdisk Provider")
    name = account.name if account else (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="新增账号时必须填写账号名称")
    try:
        result = await baidu_qr_login.start(
            account_id=account.id if account else None,
            account_name=name,
        )
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return QrLoginStartResponse(
        session_id=result.session_id,
        qr_code_data_url=result.qr_code_data_url,
        expires_in=result.expires_in,
    )


@router.get("/baidu/qr-login/{session_id}", response_model=QrLoginStatusResponse)
async def poll_baidu_qr_login(
    session_id: str, db: DbSession, _: AdminUser
) -> QrLoginStatusResponse:
    try:
        login_status, session = await baidu_qr_login.poll(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if login_status != "confirmed" or not session.cookie:
        return QrLoginStatusResponse(status=login_status)

    try:
        if session.account_id:
            account = _get_account(db, session.account_id)
            account = update_account(
                db,
                account,
                CloudAccountUpdate(refresh_token=session.cookie),
            )
        else:
            account = create_account(
                db,
                CloudAccountCreate(
                    provider="baidu",
                    name=session.account_name or "Baidu Netdisk",
                    refresh_token=session.cookie,
                ),
            )
        account = await verify_account(db, account)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="账号名称已经存在") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await baidu_qr_login.finish(session_id)
    return QrLoginStatusResponse(status="confirmed", account=account)


@router.post("/cloud-accounts/{account_id}/verify", response_model=CloudAccountRead)
async def verify(account_id: int, db: DbSession, _: AdminUser) -> CloudAccount:
    account = _get_account(db, account_id)
    try:
        return await verify_account(db, account)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.put("/cloud-accounts/{account_id}/open-credential", response_model=CloudAccountRead)
def bind_open_credential(
    account_id: int,
    payload: OpenCredentialConfigure,
    db: DbSession,
    _: AdminUser,
) -> CloudAccount:
    account = _get_account(db, account_id)
    try:
        return configure_open_credential(db, account, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/cloud-accounts/{account_id}/open-credential/verify",
    response_model=CloudAccountRead,
)
async def verify_bound_open_credential(
    account_id: int, db: DbSession, _: AdminUser
) -> CloudAccount:
    account = _get_account(db, account_id)
    try:
        return await verify_open_credential(db, account)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.delete("/cloud-accounts/{account_id}/open-credential", response_model=CloudAccountRead)
def unbind_open_credential(account_id: int, db: DbSession, _: AdminUser) -> CloudAccount:
    return remove_open_credential(db, _get_account(db, account_id))


@router.get("/cloud-accounts/{account_id}/drives", response_model=list[DriveInfo])
async def list_drives(account_id: int, db: DbSession, _: AdminUser) -> list[DriveInfo]:
    account = _get_account(db, account_id)
    provider = get_provider(account.provider, get_decrypted_token(account))
    open_provider = None
    try:
        private_profile = await provider.validate_account()
        account.provider_user_id = private_profile.user_id
        profiles = [private_profile]
        if account.open_auth_mode:
            open_provider = get_open_provider(account)
            open_profile = await open_provider.validate_account()
            if (
                private_profile.user_id
                and open_profile.user_id
                and private_profile.user_id != open_profile.user_id
            ):
                raise ValueError("OpenAPI token belongs to a different cloud account")
            account.open_account_identity = open_profile.identity
            account.open_status = "active"
            account.open_last_error = None
            account.open_last_verified_at = utcnow()
            profiles.insert(0, open_profile)
    except Exception as exc:
        if account.open_auth_mode:
            account.open_status = "error"
            account.open_last_error = str(exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        changed = persist_provider_token(account, provider)
        if open_provider is not None:
            changed = persist_open_provider_token(account, open_provider) or changed
        if changed or account.open_auth_mode:
            db.commit()
    drives: dict[str, DriveInfo] = {}
    for profile in profiles:
        for item in profile.drives:
            drives.setdefault(item.id, DriveInfo(id=item.id, name=item.name, type=item.type))
        if not profile.drives and profile.default_drive_id:
            drives.setdefault(
                profile.default_drive_id,
                DriveInfo(id=profile.default_drive_id, name="默认盘", type="default"),
            )
    return list(drives.values())


@router.get("/cloud-accounts/{account_id}/folders", response_model=list[FolderItem])
async def list_folders(
    account_id: int,
    db: DbSession,
    _: AdminUser,
    path: str = "/",
    drive_id: str | None = None,
) -> list[FolderItem]:
    account = _get_account(db, account_id)
    using_open_provider = bool(account.open_auth_mode)
    provider = (
        get_open_provider(account, drive_id)
        if using_open_provider
        else get_provider(account.provider, get_decrypted_token(account), drive_id)
    )
    try:
        folder = await provider.resolve_target_path(path)
        marker: str | None = None
        folder_items = []
        while True:
            page = await provider.list_target_items(folder, marker)
            folder_items.extend(item for item in page.items if item.item_type == "folder")
            marker = page.next_marker
            if not marker:
                break
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        token_changed = (
            persist_open_provider_token(account, provider)
            if using_open_provider
            else persist_provider_token(account, provider)
        )
        if token_changed:
            db.commit()
    return [
        FolderItem(
            id=item.remote_file_id,
            name=item.filename,
            type=item.item_type,
            size=item.size,
            updated_at=item.updated_at,
        )
        for item in folder_items
    ]
