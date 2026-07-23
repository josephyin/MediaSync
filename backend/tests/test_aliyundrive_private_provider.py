import json

import httpx

from app.providers.aliyundrive.private_provider import AliyunDrivePrivateProvider
from app.providers.base import FolderRef, RemoteItem


def make_provider(handler) -> tuple[AliyunDrivePrivateProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        AliyunDrivePrivateProvider(
            refresh_token="old-refresh-token",
            api_base_url="https://api.alipan.test",
            auth_base_url="https://auth.alipan.test",
            http_client=client,
            request_interval_seconds=0,
            retry_backoff_seconds=0,
        ),
        client,
    )


async def test_retries_rate_limited_private_request() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"code": "TooManyRequests"}
            )
        return httpx.Response(200, json={"share_token": "share-token"})

    provider, client = make_provider(handler)
    try:
        share = await provider.resolve_share("https://www.alipan.com/s/share-1")
    finally:
        await client.aclose()

    assert share.share_key == "share-1"
    assert attempts == 2


async def test_validate_account_refreshes_private_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/account/token":
            assert json.loads(request.content) == {
                "refresh_token": "old-refresh-token",
                "grant_type": "refresh_token",
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "new-refresh-token",
                },
            )
        assert request.url.path == "/v2/user/get"
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(
            200,
            json={
                "user_id": "user-1",
                "nick_name": "Media User",
                "default_drive_id": "default-drive",
                "resource_drive_id": "drive-1",
                "backup_drive_id": "backup-drive",
            },
        )

    provider, client = make_provider(handler)
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.identity == "Media User"
    assert profile.user_id == "user-1"
    assert profile.default_drive_id == "drive-1"
    assert [(drive.type, drive.id) for drive in profile.drives] == [
        ("default", "default-drive"),
        ("resource", "drive-1"),
        ("backup", "backup-drive"),
    ]
    assert provider.consume_refresh_token_update() == "new-refresh-token"
    assert provider.consume_refresh_token_update() is None
    assert [request.url.path for request in requests] == [
        "/v2/account/token",
        "/v2/user/get",
    ]


async def test_resolve_share_and_list_share_folder() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/share_link/get_share_token":
            assert json.loads(request.content) == {
                "share_id": "share-1",
                "share_pwd": "1234",
            }
            assert "Authorization" not in request.headers
            return httpx.Response(200, json={"share_token": "share-token"})
        assert request.url.path == "/adrive/v3/file/list"
        assert request.headers["x-share-token"] == "share-token"
        body = json.loads(request.content)
        assert body["share_id"] == "share-1"
        assert body["parent_file_id"] == "folder-1"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "file_id": "movie-1",
                        "parent_file_id": "folder-1",
                        "name": "movie.mkv",
                        "type": "file",
                        "size": 2048,
                        "content_hash": "abc",
                    }
                ],
                "next_marker": "next-page",
            },
        )

    provider, client = make_provider(handler)
    try:
        share = await provider.resolve_share(
            "https://www.alipan.com/s/share-1/folder/folder-1",
            "1234",
        )
        page = await provider.list_share_items(share, share.root_folder_id)
    finally:
        await client.aclose()

    assert share.share_key == "share-1"
    assert share.root_folder_id == "folder-1"
    assert page.items[0].remote_file_id == "movie-1"
    assert page.items[0].filename == "movie.mkv"
    assert page.next_marker == "next-page"
    assert [request.url.path for request in requests].count("/v2/share_link/get_share_token") == 1


async def test_resolve_target_create_folder_and_save_shared_file() -> None:
    copy_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal copy_body
        path = request.url.path
        body = json.loads(request.content)
        if path == "/v2/share_link/get_share_token":
            return httpx.Response(200, json={"share_token": "share-token"})
        if path == "/v2/account/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "old-refresh-token",
                },
            )
        if path == "/v2/user/get":
            return httpx.Response(200, json={"resource_drive_id": "drive-1"})
        if path == "/v2/file/list" and body["parent_file_id"] == "root":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "file_id": "media-folder",
                            "parent_file_id": "root",
                            "name": "Media",
                            "type": "folder",
                        }
                    ],
                    "next_marker": "",
                },
            )
        if path == "/v2/file/list":
            return httpx.Response(200, json={"items": [], "next_marker": ""})
        if path == "/adrive/v2/file/createWithFolders":
            assert body == {
                "drive_id": "drive-1",
                "parent_file_id": "media-folder",
                "name": "Movies",
                "type": "folder",
                "check_name_mode": "refuse",
            }
            return httpx.Response(200, json={"file_id": "movies-folder"})
        assert path == "/v2/file/copy"
        assert request.headers["Authorization"] == "Bearer access-token"
        assert request.headers["x-share-token"] == "share-token"
        copy_body = body
        return httpx.Response(200, json={"file_id": "saved-file"})

    provider, client = make_provider(handler)
    try:
        share = await provider.resolve_share("https://www.alipan.com/s/share-1")
        media = await provider.resolve_target_path("/Media")
        movies = await provider.ensure_folder(media, "Movies")
        result = await provider.save_shared_item(
            share,
            RemoteItem("source-file", "root", "movie.mkv", "file"),
            movies,
        )
    finally:
        await client.aclose()

    assert media == FolderRef("media-folder", "/Media")
    assert movies == FolderRef("movies-folder", "/Media/Movies")
    assert copy_body == {
        "share_id": "share-1",
        "file_id": "source-file",
        "to_drive_id": "drive-1",
        "to_parent_file_id": "movies-folder",
        "auto_rename": False,
    }
    assert result.target_file_id == "saved-file"
    assert result.target_path == "/Media/Movies/movie.mkv"
