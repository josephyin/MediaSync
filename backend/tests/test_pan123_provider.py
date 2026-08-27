import json

import httpx

from app.providers.base import FolderRef, RemoteItem
from app.providers.pan123.provider import Pan123PrivateProvider
from app.providers.registry import PROVIDERS, list_provider_types


def make_provider(handler, *, page_size: int = 2):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        Pan123PrivateProvider(
            "token-value-long-enough",
            http_client=client,
            timeout_seconds=1,
            page_size=page_size,
            retry_backoff_seconds=0,
            login_uuid="1234567890abcdef",
        ),
        client,
    )


async def test_validate_account_maps_default_drive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/b/api/user/info"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"UID": 123, "Nickname": "tester"}},
        )

    provider, client = make_provider(handler)
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.identity == "tester"
    assert profile.user_id == "123"
    assert profile.default_drive_id == "0"
    assert [(drive.id, drive.type) for drive in profile.drives] == [("0", "default")]


async def test_resolve_and_paginate_share() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/b/api/share/get"
        assert request.url.params["shareKey"] == "share-key"
        assert request.url.params["SharePwd"] == "8888"
        page = int(request.url.params["Page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "InfoList": [
                        {
                            "FileId": page,
                            "ParentFileId": 0,
                            "FileName": f"item-{page}",
                            "Size": page,
                            "Type": 0,
                            "Etag": f"etag-{page}",
                        }
                    ],
                    "Total": 2,
                    "Next": "2" if page == 1 else "-1",
                },
            },
        )

    provider, client = make_provider(handler, page_size=1)
    try:
        share = await provider.resolve_share(
            "https://www.123pan.com/s/share-key?pwd=8888"
        )
        first = await provider.list_share_items(share, "0")
        second = await provider.list_share_items(share, "0", first.next_marker)
    finally:
        await client.aclose()

    assert pages == [1, 2]
    assert first.items[0].remote_file_id == "1"
    assert first.items[0].content_hash == "etag-1"
    assert first.next_marker == "2"
    assert second.next_marker is None


async def test_create_folder_is_verified_by_listing() -> None:
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        if request.url.path == "/b/api/file/list/new":
            list_calls += 1
            items = []
            if list_calls >= 2:
                items = [
                    {
                        "FileId": 99,
                        "ParentFileId": 0,
                        "FileName": "Media",
                        "Size": 0,
                        "Type": 1,
                        "Etag": "",
                    }
                ]
            return httpx.Response(
                200,
                json={"code": 0, "data": {"InfoList": items, "Total": len(items)}},
            )
        assert request.url.path == "/b/api/file/upload_request"
        assert json.loads(request.content) == {
            "driveId": 0,
            "etag": "",
            "fileName": "Media",
            "parentFileId": "0",
            "size": 0,
            "type": 1,
            "duplicate": 1,
            "NotReuse": True,
            "event": "newCreateFolder",
            "operateType": 1,
        }
        return httpx.Response(200, json={"code": 0, "data": {}})

    provider, client = make_provider(handler)
    try:
        folder = await provider.ensure_folder(FolderRef("0", "/"), "Media")
    finally:
        await client.aclose()

    assert folder == FolderRef("99", "/Media")


async def test_resumable_save_encodes_recoverable_operation_and_verifies_target() -> None:
    target_visible = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal target_visible
        if request.url.path == "/b/api/share/get":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "InfoList": [
                            {
                                "FileId": 10,
                                "ParentFileId": 0,
                                "FileName": "probe.txt",
                                "Size": 5,
                                "Type": 0,
                                "Etag": "etag",
                            }
                        ],
                        "Total": 1,
                    },
                },
            )
        if request.url.path == "/b/api/restful/goapi/v1/file/copy/save":
            assert request.headers["LoginUuid"] == "1234567890abcdef"
            target_visible = True
            return httpx.Response(200, json={"code": 0, "data": {}})
        assert request.url.path == "/b/api/file/list/new"
        items = []
        if target_visible:
            items = [
                {
                    "FileId": 20,
                    "ParentFileId": 0,
                    "FileName": "probe.txt",
                    "Size": 5,
                    "Type": 0,
                    "Etag": "etag",
                }
            ]
        return httpx.Response(
            200,
            json={"code": 0, "data": {"InfoList": items, "Total": len(items)}},
        )

    provider, client = make_provider(handler)
    try:
        share = await provider.resolve_share("https://www.123pan.com/s/share-key")
        operation_id = await provider.start_save_shared_item(
            share,
            RemoteItem("10", "0", "probe.txt", "file", size=5, content_hash="etag"),
            FolderRef("0", "/"),
        )
        result = await provider.query_save_operation(operation_id)
    finally:
        await client.aclose()

    assert operation_id.startswith("p123v1.")
    assert result.completed is True
    assert result.target_file_ids == ("20",)


def test_pan123_provider_is_exposed_as_experimental() -> None:
    assert "pan123" in PROVIDERS
    metadata = next(item for item in list_provider_types() if item["id"] == "pan123")
    assert metadata["enabled"] is True
    assert metadata["status"] == "experimental"
    assert metadata["capabilities"] == [
        "account_verify",
        "share_browse",
        "folder_browse",
        "folder_create",
        "share_save",
    ]
