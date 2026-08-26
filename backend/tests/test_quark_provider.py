import json

import httpx
import pytest

from app.core.exceptions import ProviderCapabilityError, ProviderWriteUncertainError
from app.providers.base import FolderRef, RemoteItem, ShareInfo
from app.providers.quark.provider import QuarkPrivateProvider
from app.providers.registry import PROVIDERS, list_provider_types


def make_provider(
    handler, *, page_size: int = 2
) -> tuple[QuarkPrivateProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        QuarkPrivateProvider(
            "session=fake; __puus=old",
            http_client=client,
            timeout_seconds=1,
            page_size=page_size,
            retry_backoff_seconds=0,
        ),
        client,
    )


async def test_validate_account_maps_profile_and_exposes_rotated_cookie_once() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "drive.quark.cn"
        if request.url.path == "/1/clouddrive/member":
            assert request.headers["Cookie"] == "session=fake; __puus=old"
            return httpx.Response(
                200,
                headers={"Set-Cookie": "__puus=new; Path=/; Secure; HttpOnly"},
                json={
                    "status": 200,
                    "code": 0,
                    "data": {"member_type": "PRIVATE", "total_capacity": 1},
                },
            )
        assert request.url.path == "/1/clouddrive/file/sort"
        assert request.headers["Cookie"] == "session=fake; __puus=new"
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {"list": []},
                "metadata": {"_total": 0},
            },
        )

    provider, client = make_provider(handler)
    try:
        profile = await provider.validate_account()
        page = await provider.list_target_items(FolderRef("0", "/"))
    finally:
        await client.aclose()

    assert profile.identity == "Quark Drive"
    assert profile.user_id is None
    assert profile.default_drive_id == "0"
    assert [(drive.id, drive.type) for drive in profile.drives] == [("0", "default")]
    assert page.items == []
    assert provider.request_count == 2
    assert provider.consume_refresh_token_update() == "session=fake; __puus=new"
    assert provider.consume_refresh_token_update() is None


async def test_resolve_and_paginate_share_without_exposing_share_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/1/clouddrive/share/sharepage/token":
            assert json.loads(request.content) == {
                "pwd_id": "share_1234",
                "passcode": "1234",
            }
            return httpx.Response(
                200,
                json={"status": 200, "code": 0, "data": {"stoken": "private-stoken"}},
            )
        assert request.url.path == "/1/clouddrive/share/sharepage/detail"
        assert request.url.params["stoken"] == "private-stoken"
        assert request.url.params["pdir_fid"] == "0"
        page = int(request.url.params["_page"])
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {
                    "list": [
                        {
                            "fid": f"file-{page}",
                            "pdir_fid": "0",
                            "file_name": f"private-{page}.mkv",
                            "file": True,
                            "size": page * 100,
                            "updated_at": 1_700_000_000_000,
                            "share_fid_token": f"private-file-token-{page}",
                        }
                    ]
                },
                "metadata": {"_total": 2},
            },
        )

    provider, client = make_provider(handler, page_size=1)
    try:
        share = await provider.resolve_share(
            "https://pan.quark.cn/s/share_1234",
            "1234",
        )
        first = await provider.list_share_items(share, share.root_folder_id)
        second = await provider.list_share_items(share, share.root_folder_id, first.next_marker)
    finally:
        await client.aclose()

    assert share == ShareInfo("share_1234", "Quark share", "0")
    assert first.items[0].remote_file_id == "file-1"
    assert first.items[0].filename == "private-1.mkv"
    assert first.items[0].metadata == {"share_fid_token": "private-file-token-1"}
    assert first.next_marker == "2"
    assert second.items[0].remote_file_id == "file-2"
    assert second.next_marker is None
    assert len(requests) == 3


async def test_resolve_target_path_and_list_items_are_read_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/1/clouddrive/file/sort"
        parent_id = request.url.params["pdir_fid"]
        if parent_id == "0":
            items = [
                {
                    "fid": "media-folder",
                    "pdir_fid": "0",
                    "file_name": "Media",
                    "dir": True,
                }
            ]
        else:
            assert parent_id == "media-folder"
            items = [
                {
                    "fid": "movie-file",
                    "pdir_fid": "media-folder",
                    "file_name": "movie.mkv",
                    "file": True,
                    "size": 2048,
                }
            ]
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {"list": items},
                "metadata": {"_total": len(items)},
            },
        )

    provider, client = make_provider(handler)
    try:
        target = await provider.resolve_target_path("/Media")
        page = await provider.list_target_items(target)
        found = await provider.find_target_item(target, "movie.mkv")
    finally:
        await client.aclose()

    assert target == FolderRef("media-folder", "/Media")
    assert page.items[0].item_type == "file"
    assert found is not None
    assert found.remote_file_id == "movie-file"


async def test_legacy_share_save_is_explicitly_unavailable() -> None:
    provider = QuarkPrivateProvider("session=fake")
    target = FolderRef("0", "/")

    with pytest.raises(ProviderCapabilityError, match="share save"):
        await provider.save_shared_item(
            ShareInfo("share_1234", "Quark share", "0"),
            RemoteItem("file-1", "0", "movie.mkv", "file"),
            target,
        )


async def test_create_folder_and_resumable_share_save_protocol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/1/clouddrive/share/sharepage/token":
            return httpx.Response(
                200,
                json={"status": 200, "code": 0, "data": {"stoken": "stoken"}},
            )
        if request.url.path == "/1/clouddrive/file/sort":
            return httpx.Response(
                200,
                json={
                    "status": 200,
                    "code": 0,
                    "data": {"list": []},
                    "metadata": {"_total": 0},
                },
            )
        if request.url.path == "/1/clouddrive/file":
            assert request.url.host == "drive-pc.quark.cn"
            assert json.loads(request.content) == {
                "dir_init_lock": False,
                "dir_path": "",
                "file_name": "Media",
                "pdir_fid": "0",
            }
            return httpx.Response(
                200,
                json={"status": 200, "code": 0, "data": {"fid": "folder-1"}},
            )
        if request.url.path == "/1/clouddrive/share/sharepage/save":
            assert request.url.params["app"] == "clouddrive"
            assert request.url.params["__dt"] == "180000"
            assert float(request.url.params["__t"]) > 0
            assert json.loads(request.content) == {
                "fid_list": ["source-1"],
                "fid_token_list": ["source-token"],
                "to_pdir_fid": "folder-1",
                "pwd_id": "share_1234",
                "stoken": "stoken",
                "pdir_fid": "0",
                "scene": "link",
            }
            return httpx.Response(
                200,
                json={"status": 200, "code": 0, "data": {"task_id": "task-1"}},
            )
        assert request.url.path == "/1/clouddrive/task"
        assert request.url.params["task_id"] == "task-1"
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {
                    "status": 2,
                    "save_as": {"save_as_top_fids": ["saved-1"]},
                },
            },
        )

    provider, client = make_provider(handler)
    try:
        share = await provider.resolve_share("https://pan.quark.cn/s/share_1234")
        target = await provider.ensure_folder(FolderRef("0", "/"), "Media")
        operation_id = await provider.start_save_shared_item(
            share,
            RemoteItem(
                "source-1",
                "0",
                "movie.mkv",
                "file",
                metadata={"share_fid_token": "source-token"},
            ),
            target,
        )
        result = await provider.query_save_operation(operation_id)
    finally:
        await client.aclose()

    assert target == FolderRef("folder-1", "/Media")
    assert result.completed is True
    assert result.target_file_ids == ("saved-1",)


async def test_share_save_transport_failure_is_marked_uncertain_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/1/clouddrive/share/sharepage/token":
            return httpx.Response(
                200,
                json={"status": 200, "code": 0, "data": {"stoken": "stoken"}},
            )
        attempts += 1
        raise httpx.ReadTimeout("secret URL must not escape")

    provider, client = make_provider(handler)
    try:
        share = await provider.resolve_share("https://pan.quark.cn/s/share_1234")
        with pytest.raises(ProviderWriteUncertainError):
            await provider.start_save_shared_item(
                share,
                RemoteItem(
                    "source-1",
                    "0",
                    "movie.mkv",
                    "file",
                    metadata={"share_fid_token": "source-token"},
                ),
                FolderRef("target-1", "/Media"),
            )
    finally:
        await client.aclose()

    assert attempts == 1


def test_quark_private_adapter_exposes_live_verified_write_capabilities() -> None:
    assert "quark" in PROVIDERS
    metadata = next(item for item in list_provider_types() if item["id"] == "quark")
    assert metadata["enabled"] is True
    assert metadata["status"] == "experimental"
    assert metadata["capabilities"] == [
        "account_verify",
        "share_browse",
        "folder_browse",
        "folder_create",
        "share_save",
    ]


async def test_readonly_provider_retries_one_temporary_failure() -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"status": 503, "code": 503})
        return httpx.Response(
            200,
            json={
                "status": 200,
                "code": 0,
                "data": {"member_type": "PRIVATE"},
            },
        )

    provider, client = make_provider(handler)
    try:
        await provider.validate_account()
    finally:
        await client.aclose()

    assert attempts == 2
    assert provider.request_count == 2
