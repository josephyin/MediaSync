import httpx

from app.providers.pan123.open_provider import Pan123OpenProvider


async def test_openlist_refresh_validates_account_and_rotates_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.oplist.org":
            assert request.url.params["refresh_ui"] == "old-refresh"
            assert request.url.params["driver_txt"] == "123cloud_oa"
            return httpx.Response(
                200,
                json={
                    "access_token": "access-value",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            )
        assert request.url.path == "/api/v1/user/info"
        assert request.headers["authorization"] == "Bearer access-value"
        assert request.headers["platform"] == "open_platform"
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"uid": 12345}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Pan123OpenProvider(
            refresh_token="old-refresh",
            oauth_token_url="https://api.oplist.org/123cloud/renewapi",
            http_client=client,
        )
        profile = await provider.validate_account()

    assert profile.identity == "12345"
    assert profile.user_id == "12345"
    assert provider.consume_refresh_token_update() == "new-refresh"
    assert provider.consume_refresh_token_update() is None


async def test_custom_credentials_list_the_official_account_drive() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/access_token":
            assert request.method == "POST"
            assert request.headers["platform"] == "open_platform"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "accessToken": "developer-access",
                        "expiredAt": "2026-08-29T00:00:00+08:00",
                    },
                },
            )
        assert request.url.path == "/api/v2/file/list"
        assert request.url.params["parentFileId"] == "0"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "ok",
                "data": {
                    "lastFileId": -1,
                    "fileList": [
                        {
                            "fileId": 88,
                            "parentFileId": 0,
                            "filename": "Media",
                            "type": 1,
                            "size": 0,
                            "etag": "",
                            "updateAt": "2026-08-28 10:00:00",
                            "trashed": 0,
                        }
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = Pan123OpenProvider(
            client_id="client-id",
            client_secret="client-secret",
            http_client=client,
        )
        page = await provider.list_target_items(await provider.resolve_target_path("/"))

    assert page.next_marker is None
    assert [(item.remote_file_id, item.filename, item.item_type) for item in page.items] == [
        ("88", "Media", "folder")
    ]
