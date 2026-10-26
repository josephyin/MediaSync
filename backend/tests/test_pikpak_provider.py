import json

import httpx
import pytest

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderPaginationError,
    ProviderRequestError,
    ProviderWriteUncertainError,
)
from app.providers.base import FolderRef, RemoteItem, ResumableSaveProvider
from app.providers.pikpak.provider import PikPakProvider, parse_share_url
from app.providers.registry import get_provider, list_provider_types
from app.services.transfer_operation import TransferSpec, execute_transfer


def file(file_id="source", **overrides):
    return {
        "id": file_id,
        "name": "movie.mkv",
        "kind": "drive#file",
        "size": "42",
        "hash": "sha1-content",
        "parent_id": "target",
        "phase": "PHASE_TYPE_COMPLETE",
        **overrides,
    }


@pytest.mark.parametrize("matches", [False, True])
async def test_existing_target_requires_complete_matching_fingerprint(matches):
    def respond(request):
        if request.url.path == "/drive/v1/share":
            return httpx.Response(200, json={"share_status": "OK", "pass_code_token": "secret"})
        if request.url.path == "/drive/v1/files":
            return httpx.Response(
                200,
                json={
                    "files": [
                        file(
                            "existing",
                            parent_id="",
                            hash="sha1-content" if matches else "other-content",
                        )
                    ]
                },
            )
        raise AssertionError("Existing files must never cause another restore request")

    adapter, client, _requests = provider(respond)
    spec = TransferSpec(
        "https://mypikpak.com/s/share",
        None,
        "/",
        "source",
        None,
        "movie.mkv",
        "movie.mkv",
        "file",
        42,
        "sha1-content",
    )
    async with client:
        if matches:
            result = await execute_transfer(adapter, spec)
            assert result.already_existed and result.target_file_id == "existing"
        else:
            with pytest.raises(ProviderRequestError, match="fingerprint"):
                await execute_transfer(adapter, spec)


def provider(handler, credential="private-refresh"):
    requests = []

    def dispatch(request):
        requests.append(request)
        if request.url.path == "/v1/auth/token":
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "access_token": "private-access",
                    "refresh_token": "rotated-refresh",
                    "sub": "user1",
                    "expires_in": 7200,
                },
            )
        if request.url.path == "/v1/shield/captcha/init":
            return httpx.Response(200, json={"captcha_token": "private-captcha", "expires_in": 300})
        assert request.headers["X-Captcha-Token"] == "private-captcha"
        assert request.headers["Authorization"] == "Bearer private-access"
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    return PikPakProvider(credential, http_client=client), client, requests


async def test_profile_and_encrypted_credential_roundtrip():
    adapter, client, requests = provider(lambda _: httpx.Response(200, json={"quota": {}}))
    async with client:
        profile = await adapter.validate_account()
        await adapter.validate_account()
    assert profile.user_id == "user1"
    assert profile.default_drive_id == "root"
    assert sum(r.url.path == "/v1/auth/token" for r in requests) == 1
    updated = adapter.consume_refresh_token_update()
    credential = json.loads(updated)
    assert credential["refresh_token"] == "rotated-refresh"
    assert "access_token" not in credential
    assert adapter.consume_refresh_token_update() is None
    restored = PikPakProvider(updated)
    assert restored._credentials["device_id"] == credential["device_id"]


@pytest.mark.parametrize(
    "value",
    ["", "{broken", '{"refresh_token":1}', '{"refresh_token":"x","client_id":"bad\\nvalue"}'],
)
def test_reject_invalid_credentials_without_echo(value):
    with pytest.raises(ProviderRequestError) as exc:
        PikPakProvider(value)
    if value:
        assert value not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://mypikpak.com/s/abc",
        "https://evil.test/s/abc",
        "https://mypikpak.com.evil.test/s/abc",
        "https://me@mypikpak.com/s/abc",
        "https://mypikpak.com:444/s/abc",
        "https://mypikpak.com/s/a/%2F",
        "https://mypikpak.com/s/a/b/c",
    ],
)
def test_reject_untrusted_share_urls(url):
    with pytest.raises(ProviderRequestError):
        parse_share_url(url)


def test_share_url_subfolder():
    assert parse_share_url("https://mypikpak.com/s/abc") == ("abc", "root")
    assert parse_share_url("https://mypikpak.com/s/abc/folder/") == ("abc", "folder")


async def test_share_password_and_nested_pagination():
    def handler(request):
        if request.url.path == "/drive/v1/share":
            assert request.url.params["pass_code"] == "1234"
            return httpx.Response(
                200,
                json={
                    "share_status": "OK",
                    "title": "Movies",
                    "pass_code_token": "private-share-token",
                },
            )
        assert request.url.path == "/drive/v1/share/detail"
        assert request.url.params["parent_id"] == "folder"
        assert request.url.params["pass_code_token"] == "private-share-token"
        if request.url.params["page_token"]:
            return httpx.Response(200, json={"files": [], "next_page_token": "stale"})
        return httpx.Response(200, json={"files": [file()], "next_page_token": "next"})

    adapter, client, _ = provider(handler)
    async with client:
        share = await adapter.resolve_share("https://mypikpak.com/s/abc/folder", "1234")
        page = await adapter.list_share_items(share, share.root_folder_id)
        last = await adapter.list_share_items(share, share.root_folder_id, page.next_marker)
    assert page.next_marker == "next"
    assert last.next_marker is None
    assert page.items[0].size == 42
    assert "private-share-token" not in repr(share)
    assert "private-share-token" not in repr(page)


@pytest.mark.parametrize(
    "payload", [{}, {"files": {}}, {"files": [{}]}, {"files": [file()], "next_page_token": 123}]
)
def test_invalid_list_response_fails_closed(payload):
    with pytest.raises(ProviderRequestError):
        PikPakProvider._page(payload, None)


def test_empty_and_repeated_page():
    assert PikPakProvider._page({"files": [], "next_page_token": "same"}, "same").items == []
    with pytest.raises(ProviderPaginationError):
        PikPakProvider._page({"files": [file()], "next_page_token": "same"}, "same")


@pytest.mark.parametrize("cycle", [False, True])
async def test_internal_lookup_cannot_loop(cycle):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200,
            json={
                "files": [file(str(count) if cycle else "repeated")],
                "next_page_token": "a" if count % 2 else "b",
            },
        )

    adapter, client, _ = provider(handler)
    async with client:
        with pytest.raises(ProviderPaginationError):
            await adapter.find_target_item(FolderRef("root", "/"), "missing")
    assert count <= 3


async def test_folder_creation_and_existing_folder():
    writes = 0

    def handler(request):
        nonlocal writes
        if request.method == "POST":
            writes += 1
            assert json.loads(request.content) == {
                "kind": "drive#folder",
                "name": "Movies",
                "parent_id": "",
            }
            return httpx.Response(
                200, json={"file": file("movies", name="Movies", kind="drive#folder", parent_id="")}
            )
        return httpx.Response(
            200,
            json={
                "files": []
                if not writes
                else [file("movies", name="Movies", kind="drive#folder", parent_id="")]
            },
        )

    adapter, client, _ = provider(handler)
    async with client:
        first = await adapter.ensure_folder(FolderRef("root", "/"), "Movies")
        second = await adapter.ensure_folder(FolderRef("root", "/"), "Movies")
        resolved = await adapter.resolve_target_path("/Movies")
    assert first == second == resolved == FolderRef("movies", "/Movies")
    assert writes == 1


@pytest.mark.parametrize("failure", ["timeout", "500", "429", "html", "redirect", "unknown"])
async def test_uncertain_write_never_retried_or_leaks_credentials(failure):
    writes = 0

    def handler(request):
        nonlocal writes
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        writes += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("private-refresh private-share-token")
        if failure in ("500", "429"):
            return httpx.Response(int(failure), json={"error_description": "private-refresh"})
        if failure == "html":
            return httpx.Response(200, text="private-refresh")
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.test"}, json={})
        return httpx.Response(200, json={"restore_status": "unexpected-private-refresh"})

    adapter, client, _ = provider(handler)
    adapter._share_tokens["share"] = "private-share-token"
    from app.providers.base import ShareInfo

    async with client:
        with pytest.raises(ProviderWriteUncertainError) as exc:
            await adapter.start_save_shared_item(
                ShareInfo("share", "Share"),
                PikPakProvider._item(file()),
                FolderRef("target", "/Movies"),
            )
    assert writes == 1
    assert "private-refresh" not in str(exc.value)
    assert "private-share-token" not in str(exc.value)


async def test_transfer_resume_uses_persisted_operation_and_only_one_post():
    from app.core.exceptions import ProviderOperationPendingError

    restored = False
    complete = False
    writes = 0

    def handler(request):
        nonlocal restored, writes
        if request.url.path == "/drive/v1/share":
            return httpx.Response(200, json={"share_status": "OK", "pass_code_token": "secret"})
        if request.url.path == "/drive/v1/share/restore":
            writes += 1
            body = json.loads(request.content)
            assert "to" not in body
            assert body["file_ids"] == ["source"]
            restored = True
            return httpx.Response(200, json={"restore_status": "RESTORE_START"})
        return httpx.Response(
            200, json={"files": [file("saved", parent_id="")] if complete else []}
        )

    spec = TransferSpec(
        "https://mypikpak.com/s/share",
        None,
        "/",
        "source",
        "root",
        "movie.mkv",
        "movie.mkv",
        "file",
        42,
        "sha1-content",
    )
    operations = []
    intents = []

    async def record(op):
        operations.append(op)

    async def intent():
        intents.append(True)

    adapter, client, _ = provider(handler)
    async with client:
        with pytest.raises(ProviderOperationPendingError):
            await execute_transfer(
                adapter, spec, record_provider_operation=record, record_write_intent=intent
            )
    assert restored and writes == 1
    assert intents == [True]
    assert len(operations) == 1
    assert "secret" not in operations[0]
    assert len(operations[0]) <= 255
    complete = True
    # A new provider simulates a worker restart with only the stored operation ID.
    adapter, client, _ = provider(handler)
    async with client:
        result = await execute_transfer(adapter, spec, provider_operation_id=operations[0])
    assert result.target_file_id == "saved"
    assert result.target_path == "/movie.mkv"
    assert writes == 1


async def test_result_requires_unique_complete_fingerprint():
    items = []
    adapter, client, _ = provider(lambda _: httpx.Response(200, json={"files": items}))
    op = "pikpak1.dGFyZ2V0." + adapter._fingerprint(adapter._item(file()))
    async with client:
        items[:] = [file("saved", phase="PHASE_TYPE_RUNNING")]
        assert not (await adapter.query_save_operation(op)).completed
        items[:] = [file("saved", hash="wrong-content")]
        assert not (await adapter.query_save_operation(op)).completed
        items[:] = [file("one"), file("two")]
        with pytest.raises(ProviderWriteUncertainError):
            await adapter.query_save_operation(op)


async def test_save_requires_hash_and_does_not_use_nonresumable_contract():
    from app.providers.base import ShareInfo

    adapter = PikPakProvider("token")
    adapter._share_tokens["share"] = ""
    share = ShareInfo("share", "Share")
    source = RemoteItem("id", None, "movie", "file")
    with pytest.raises(ProviderCapabilityError):
        await adapter.start_save_shared_item(share, source, FolderRef("root", "/"))
    with pytest.raises(ProviderCapabilityError):
        await adapter.save_shared_item(share, source, FolderRef("root", "/"))
    assert adapter.request_count == 0


def test_registry():
    adapter = get_provider("pikpak", "token", "root")
    assert isinstance(adapter, ResumableSaveProvider)
    info = next(p for p in list_provider_types() if p["id"] == "pikpak")
    assert info["enabled"] is True
    assert info["status"] == "experimental"
    assert {"account_verify", "share_browse", "folder_browse", "share_save"} <= set(
        info["capabilities"]
    )
    with pytest.raises(Exception, match="default drive"):
        get_provider("pikpak", "token", "other")


async def test_folder_picker_direct_pagination_detects_cycles_and_resets():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(
            200, json={"files": [file(str(count))], "next_page_token": "a" if count % 2 else "b"}
        )

    adapter, client, _ = provider(handler)
    async with client:
        target = FolderRef("target", "/")
        await adapter.list_target_items(target)
        await adapter.list_target_items(target, "a")
        with pytest.raises(ProviderPaginationError):
            await adapter.list_target_items(target, "b")
        # A fresh independent listing may legitimately return the same cursor.
        await adapter.list_target_items(target)


async def test_wrong_parent_does_not_report_save_success():
    adapter, client, _ = provider(
        lambda _: httpx.Response(
            200,
            json={
                "files": [file("saved", parent_id="other")],
            },
        )
    )
    operation = "pikpak1.dGFyZ2V0." + adapter._fingerprint(adapter._item(file()))
    async with client:
        with pytest.raises(ProviderWriteUncertainError, match="outside"):
            await adapter.query_save_operation(operation)


@pytest.mark.parametrize(
    "status,payload",
    [
        (401, {"error_description": "private-refresh"}),
        (403, {"error": "captcha_required", "error_description": "private-refresh"}),
        (200, {"error_code": 4126, "error_description": "private-refresh"}),
    ],
)
async def test_authorization_errors_are_sanitized(status, payload):
    adapter, client, _ = provider(lambda _: httpx.Response(status, json=payload))
    async with client:
        with pytest.raises(ProviderRequestError, match="official website") as exc:
            await adapter.validate_account()
    assert "private-refresh" not in str(exc.value)


async def test_token_rotation_persisted_encrypted_even_after_upstream_error():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.core.security import get_credential_cipher
    from app.models import Base, CloudAccount
    from app.services.account_service import get_decrypted_token, persist_provider_token

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    adapter, client, _ = provider(lambda _: httpx.Response(503, json={}))
    async with client:
        with pytest.raises(ProviderRequestError):
            await adapter.validate_account()
    with Session(engine) as db:
        account = CloudAccount(
            provider="pikpak",
            name="PikPak test",
            status="active",
            refresh_token=get_credential_cipher().encrypt("old"),
        )
        db.add(account)
        assert persist_provider_token(account, adapter)
        db.commit()
        db.refresh(account)
        assert "rotated-refresh" not in account.refresh_token
        assert json.loads(get_decrypted_token(account))["refresh_token"] == "rotated-refresh"
        assert not persist_provider_token(account, adapter)
    engine.dispose()


async def test_web_drive_captcha_initialized_once_and_renewed_when_expired():
    adapter, client, requests = provider(lambda _: httpx.Response(200, json={"quota": {}}))
    async with client:
        await adapter.validate_account()
        await adapter.validate_account()
        init = [r for r in requests if r.url.path == "/v1/shield/captcha/init"]
        assert len(init) == 1
        body = json.loads(init[0].content)
        assert body["action"] == "GET:/drive/v1/about"
        assert body["meta"]["user_id"] == "user1"
        assert body["meta"]["captcha_sign"].startswith("1.")
        assert len(body["meta"]["captcha_sign"]) == 34
        assert body["device_id"] == adapter._credentials["device_id"]
        adapter._captcha_expires_at = 0
        await adapter.validate_account()
    assert len([r for r in requests if r.url.path == "/v1/shield/captcha/init"]) == 2
    assert "private-captcha" not in adapter.consume_refresh_token_update()


async def test_drive_challenge_stops_before_drive_access():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/auth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "sub": "user",
                    "expires_in": 300,
                },
            )
        assert request.url.path == "/v1/shield/captcha/init"
        return httpx.Response(200, json={"captcha_token": "pending", "url": "https://example.com"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = PikPakProvider("refresh", http_client=client)
        with pytest.raises(ProviderRequestError, match="人机验证"):
            await adapter.validate_account()
    assert len(requests) == 2


async def test_share_rejection_identifies_stage_without_exposing_secret():
    adapter, client, _ = provider(
        lambda _: httpx.Response(
            400,
            json={
                "error": "captcha_invalid",
                "error_code": 9,
                "error_description": "private-share-token private-password",
            },
        )
    )
    async with client:
        with pytest.raises(ProviderRequestError) as exc:
            await adapter.resolve_share(
                "https://mypikpak.com/s/private-share-id", "private-password"
            )
    message = str(exc.value)
    assert "读取分享信息" in message
    assert "验证令牌无效" in message
    assert "HTTP 400" in message and "错误码 9" in message
    assert "private-" not in message


async def test_share_root_reuses_metadata_and_paginates_without_empty_detail_id():
    def handler(request):
        assert request.url.path == "/drive/v1/share"
        assert request.url.params["pass_code"] == "password"
        if request.url.params.get("page_token"):
            assert request.url.params["page_token"] == "next"
            return httpx.Response(200, json={"share_status": "OK", "files": [file("second")]})
        return httpx.Response(
            200,
            json={
                "share_status": "OK",
                "files": [file("first")],
                "next_page_token": "next",
                "pass_code_token": "secret",
            },
        )

    adapter, client, requests = provider(handler)
    async with client:
        share = await adapter.resolve_share("https://mypikpak.com/s/share", "password")
        first = await adapter.list_share_items(share, share.root_folder_id)
        assert first.items[0].remote_file_id == "first"
        second = await adapter.list_share_items(share, "root", first.next_marker)
        assert second.items[0].remote_file_id == "second"
        assert second.next_marker is None
    assert sum(r.url.path == "/drive/v1/share" for r in requests) == 2
    assert not any(r.url.path == "/drive/v1/share/detail" for r in requests)


@pytest.mark.parametrize("duplicate", [False, True])
async def test_restore_moves_unique_fingerprint_from_receiving_folder(duplicate):
    moved = False
    writes = 0

    def handler(request):
        nonlocal moved, writes
        if request.method == "POST":
            assert request.url.path == "/drive/v1/files:batchMove"
            assert json.loads(request.content) == {"ids": ["saved"], "to": {"parent_id": "target"}}
            writes += 1
            moved = True
            return httpx.Response(200, json={})
        parent = request.url.params.get("parent_id")
        if parent == "target":
            items = [file("saved")] if moved else []
        elif parent == "inbox":
            items = [file("saved", parent_id="inbox")]
            if duplicate:
                items.append(file("duplicate", parent_id="inbox"))
        else:
            items = [file("inbox", name="Pack From Shared", kind="drive#folder", parent_id="")]
        return httpx.Response(200, json={"files": items})

    adapter, client, _ = provider(handler)
    op = "pikpak1.dGFyZ2V0." + adapter._fingerprint(adapter._item(file()))
    async with client:
        if duplicate:
            with pytest.raises(ProviderWriteUncertainError):
                await adapter.query_save_operation(op)
            assert writes == 0
        else:
            result = await adapter.query_save_operation(op)
            assert result.completed and result.target_file_ids == ("saved",)
            assert (await adapter.query_save_operation(op)).completed
            assert writes == 1
