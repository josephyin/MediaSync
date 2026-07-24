import json

import httpx
import pytest

from app.core.exceptions import ProviderCapabilityError
from app.providers.aliyundrive.provider import AliyunDriveProvider
from app.providers.base import FolderRef, RemoteItem, ShareInfo


def make_provider(handler) -> tuple[AliyunDriveProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AliyunDriveProvider(
        refresh_token="old-refresh-token",
        client_id="client-id",
        client_secret="client-secret",
        http_client=client,
    )
    return provider, client


async def test_validate_account_refreshes_token_and_reads_drive_info() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/access_token":
            assert json.loads(request.content) == {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "refresh_token",
                "refresh_token": "old-refresh-token",
            }
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "new-refresh-token"},
            )
        assert request.headers["Authorization"] == "Bearer access-token"
        return httpx.Response(
            200,
            json={
                "user_id": "user-1",
                "default_drive_id": "default-drive",
                "resource_drive_id": "resource-drive",
                "backup_drive_id": "backup-drive",
            },
        )

    provider, client = make_provider(handler)
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.identity == "user-1"
    assert profile.user_id == "user-1"
    assert profile.default_drive_id == "resource-drive"
    assert [(drive.type, drive.id) for drive in profile.drives] == [
        ("default", "default-drive"),
        ("resource", "resource-drive"),
        ("backup", "backup-drive"),
    ]
    assert provider.consume_refresh_token_update() == "new-refresh-token"
    assert provider.consume_refresh_token_update() is None
    assert [request.url.path for request in requests] == [
        "/oauth/access_token",
        "/adrive/v1.0/user/getDriveInfo",
    ]


async def test_validate_account_with_alistgo_hosted_oauth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth.alistgo.test":
            assert request.method == "GET"
            assert request.url.params["refresh_ui"] == "hosted-refresh-token"
            assert request.url.params["server_use"] == "true"
            assert request.url.params["driver_txt"] == "alicloud_qr"
            return httpx.Response(
                200,
                json={
                    "access_token": "hosted-access-token",
                    "refresh_token": "rotated-hosted-token",
                },
            )
        assert request.url.path == "/adrive/v1.0/user/getDriveInfo"
        assert request.headers["Authorization"] == "Bearer hosted-access-token"
        return httpx.Response(
            200,
            json={
                "user_id": "user-1",
                "default_drive_id": "default-drive",
                "resource_drive_id": "resource-drive",
                "backup_drive_id": "backup-drive",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AliyunDriveProvider(
        refresh_token="hosted-refresh-token",
        client_id="",
        client_secret="",
        oauth_token_url="https://oauth.alistgo.test/token",
        http_client=client,
    )
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.user_id == "user-1"
    assert {drive.type for drive in profile.drives} == {"default", "resource", "backup"}
    assert provider.consume_refresh_token_update() == "rotated-hosted-token"


async def test_alistgo_hosted_oauth_falls_back_to_legacy_post() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "oauth.alistgo.test" and request.method == "GET":
            return httpx.Response(400, json={"text": "Incorrect GrantType"})
        if request.url.host == "oauth.alistgo.test":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "client_id": "",
                "client_secret": "",
                "grant_type": "refresh_token",
                "refresh_token": "hosted-refresh-token",
            }
            return httpx.Response(
                200,
                json={
                    "access_token": "legacy-access-token",
                    "refresh_token": "rotated-legacy-token",
                },
            )
        assert request.headers["Authorization"] == "Bearer legacy-access-token"
        return httpx.Response(200, json={"user_id": "user-1", "resource_drive_id": "drive-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AliyunDriveProvider(
        refresh_token="hosted-refresh-token",
        client_id="",
        client_secret="",
        oauth_token_url="https://oauth.alistgo.test/token",
        http_client=client,
    )
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.user_id == "user-1"
    assert provider.consume_refresh_token_update() == "rotated-legacy-token"
    assert [(request.method, request.url.host) for request in requests] == [
        ("GET", "oauth.alistgo.test"),
        ("POST", "oauth.alistgo.test"),
        ("POST", "openapi.alipan.com"),
    ]


async def test_validate_account_with_openlist_apipages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.oplist.test":
            assert request.method == "GET"
            assert request.url.path == "/alicloud/renewapi"
            assert request.url.params["refresh_ui"] == "openlist-refresh-token"
            assert request.url.params["server_use"] == "true"
            assert request.url.params["driver_txt"] == "alicloud_qr"
            return httpx.Response(
                200,
                json={
                    "access_token": "openlist-access-token",
                    "refresh_token": "rotated-openlist-token",
                },
            )
        assert request.headers["Authorization"] == "Bearer openlist-access-token"
        return httpx.Response(
            200,
            json={"user_id": "user-1", "resource_drive_id": "resource-drive"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AliyunDriveProvider(
        refresh_token="openlist-refresh-token",
        client_id="",
        client_secret="",
        oauth_token_url="https://api.oplist.test/alicloud/renewapi",
        http_client=client,
    )
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.user_id == "user-1"
    assert profile.default_drive_id == "resource-drive"
    assert provider.consume_refresh_token_update() == "rotated-openlist-token"


async def test_resolve_and_list_target_folder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/access_token":
            return httpx.Response(
                200,
                json={"access_token": "access-token", "refresh_token": "old-refresh-token"},
            )
        if request.url.path == "/adrive/v1.0/user/getDriveInfo":
            return httpx.Response(200, json={"resource_drive_id": "drive-1"})
        body = json.loads(request.content)
        if body["parent_file_id"] == "root":
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
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "file_id": "movie-1",
                        "parent_file_id": "media-folder",
                        "name": "movie.mkv",
                        "type": "file",
                        "size": 1024,
                    }
                ],
                "next_marker": "",
            },
        )

    provider, client = make_provider(handler)
    try:
        folder = await provider.resolve_target_path("/Media")
        page = await provider.list_target_items(folder)
    finally:
        await client.aclose()

    assert folder == FolderRef(folder_id="media-folder", path="/Media")
    assert page.items[0].filename == "movie.mkv"


async def test_share_operations_are_explicitly_unavailable() -> None:
    provider, client = make_provider(lambda _: httpx.Response(500))
    try:
        share = await provider.resolve_share("https://www.alipan.com/s/share-1/folder/root")
        with pytest.raises(ProviderCapabilityError):
            await provider.list_share_items(share, "root")
        with pytest.raises(ProviderCapabilityError):
            await provider.save_shared_item(
                ShareInfo("share-1", "Share"),
                RemoteItem("file-1", "root", "movie.mkv", "file"),
                FolderRef("root", "/"),
            )
    finally:
        await client.aclose()
