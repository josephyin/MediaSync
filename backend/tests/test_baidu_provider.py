import httpx

from app.providers.baidu.open_provider import BaiduOpenProvider
from app.providers.baidu.provider import BaiduPrivateProvider, BaiduProvider
from app.providers.base import FolderRef, RemoteItem
from app.providers.registry import PROVIDERS, list_provider_types


async def test_baidu_open_provider_refreshes_and_lists_account_drive() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.oplist.test":
            assert request.url.path == "/baiduyun/renewapi"
            assert request.url.params["driver_txt"] == "baiduyun_go"
            assert request.url.params["refresh_ui"] == "refresh-old"
            return httpx.Response(
                200,
                json={"access_token": "access-token-long-enough", "refresh_token": "refresh-new"},
            )
        if request.url.path == "/rest/2.0/xpan/nas":
            return httpx.Response(200, json={"errno": 0, "uk": 12, "netdisk_name": "Baidu account"})
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "list": [
                    {
                        "fs_id": 34,
                        "server_filename": "Media",
                        "path": "/Media",
                        "isdir": 1,
                        "size": 0,
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BaiduOpenProvider(
        "refresh-old",
        oauth_token_url="https://api.oplist.test/baiduyun/renewapi",
        http_client=client,
    )
    try:
        profile = await provider.validate_account()
        page = await provider.list_target_items(FolderRef("/", "/"))
    finally:
        await client.aclose()

    assert profile.user_id == "12"
    assert profile.default_drive_id == "/"
    assert page.items[0].remote_file_id == "34"
    assert page.items[0].item_type == "folder"
    assert provider.consume_refresh_token_update() == "refresh-new"
    assert len([request for request in requests if request.url.host == "api.oplist.test"]) == 1


async def test_baidu_private_provider_lists_share_root() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "tieba.baidu.com":
            return httpx.Response(200, json={"data": {"user_id": 12, "user_name": "Baidu"}})
        return httpx.Response(
            200,
            json={
                "errno": 0,
                "data": {
                    "shareid": 10,
                    "uk": 20,
                    "seckey": "secret",
                    "list": [
                        {
                            "fs_id": 30,
                            "server_filename": "Episode.mp4",
                            "path": "/Episode.mp4",
                            "isdir": 0,
                            "size": 100,
                        }
                    ],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = BaiduPrivateProvider("BDUSS=session-value", http_client=client)
    try:
        profile = await provider.validate_account()
        share = await provider.resolve_share("https://pan.baidu.com/s/1share-key")
        page = await provider.list_share_items(share, share.root_folder_id)
    finally:
        await client.aclose()

    assert profile.user_id == "12"
    assert page.items[0].filename == "Episode.mp4"
    assert page.items[0].parent_id == "root"


async def test_hybrid_provider_resumes_by_verifying_target_without_resubmission() -> None:
    class FakeOpen:
        request_count = 0

        async def list_target_items(self, target, marker=None):
            from app.providers.base import RemotePage

            return RemotePage([RemoteItem("99", target.folder_id, "Episode.mp4", "file", size=100)])

        def consume_refresh_token_update(self):
            return "refresh-new"

    provider = BaiduProvider.__new__(BaiduProvider)
    provider._open = FakeOpen()
    provider._private_request_count = 0
    operation_id = provider._operation_id(
        "/Media", provider._fingerprint(RemoteItem("30", "root", "Episode.mp4", "file", 100))
    )

    operation = await provider.query_save_operation(operation_id)

    assert operation.completed is True
    assert operation.target_file_ids == ("99",)
    assert provider.consume_open_refresh_token_update() == "refresh-new"


def test_baidu_provider_is_exposed_as_experimental_hybrid() -> None:
    assert "baidu" in PROVIDERS
    metadata = next(item for item in list_provider_types() if item["id"] == "baidu")
    assert metadata["enabled"] is True
    assert metadata["mode"] == "hybrid_api"
    assert "share_save" in metadata["capabilities"]
