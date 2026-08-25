import hashlib
import json

import httpx
import pytest

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderNotConfiguredError,
    ProviderRequestError,
)
from app.providers.base import FolderRef, RemoteItem, ShareInfo
from app.providers.quark import open_cli
from app.providers.quark.open_provider import QuarkOpenProvider


def make_provider(handler, *, app_id: str = "app-id", sign_key: str = "sign-key"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = QuarkOpenProvider(
        refresh_token="old-refresh-token",
        app_id=app_id,
        sign_key=sign_key,
        oauth_token_url="https://api.oplist.test/quarkyun/renewapi",
        http_client=client,
        clock_ms=lambda: 1_700_000_000_123,
        request_id_factory=lambda: "request-id",
    )
    return provider, client


async def test_validate_account_refreshes_and_signs_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.oplist.test":
            assert request.method == "GET"
            assert request.url.params["refresh_ui"] == "old-refresh-token"
            assert request.url.params["server_use"] == "true"
            assert request.url.params["driver_txt"] == "quarkyun_oa"
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "refresh_token": "new-refresh-token",
                },
            )

        assert request.url == httpx.URL(
            "https://open-api-drive.quark.cn/open/v1/user/info",
            params={"req_id": "request-id", "access_token": "access-token"},
        )
        expected = hashlib.sha256(
            b"GET&/open/v1/user/info&1700000000123&sign-key"
        ).hexdigest()
        assert request.headers["x-pan-tm"] == "1700000000123"
        assert request.headers["x-pan-token"] == expected
        assert request.headers["x-pan-client-id"] == "app-id"
        return httpx.Response(
            200,
            json={
                "status": 0,
                "data": {"user_id": "quark-user", "nickname": "Quark account"},
            },
        )

    provider, client = make_provider(handler)
    try:
        profile = await provider.validate_account()
    finally:
        await client.aclose()

    assert profile.identity == "Quark account"
    assert profile.user_id == "quark-user"
    assert profile.default_drive_id == "0"
    assert [(item.id, item.type) for item in profile.drives] == [("0", "default")]
    assert provider.consume_refresh_token_update() == "new-refresh-token"
    assert provider.consume_refresh_token_update() is None
    assert provider.request_count == 2


async def test_missing_app_credentials_are_rejected_before_refresh() -> None:
    provider, client = make_provider(
        lambda _: httpx.Response(500), app_id="", sign_key=""
    )
    try:
        with pytest.raises(ProviderNotConfiguredError, match="AppID and SignKey"):
            await provider.validate_account()
        assert provider.request_count == 0
    finally:
        await client.aclose()


async def test_list_target_items_maps_cursor_and_files() -> None:
    list_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.oplist.test":
            return httpx.Response(
                200,
                json={"access_token": "access", "refresh_token": "old-refresh-token"},
            )
        body = json.loads(request.content)
        list_bodies.append(body)
        if len(list_bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "status": 0,
                    "data": {
                        "file_list": [
                            {
                                "fid": "folder-1",
                                "parent_fid": "0",
                                "filename": "Media",
                                "file_type": "0",
                                "size": 0,
                                "updated_at": 1_700_000_000_000,
                            }
                        ],
                        "last_page": False,
                        "next_query_cursor": {"version": "1", "token": "next-token"},
                    },
                },
            )
        assert body["query_cursor"] == {"version": "1", "token": "next-token"}
        return httpx.Response(
            200,
            json={
                "status": 0,
                "data": {
                    "file_list": [
                        {
                            "fid": "file-1",
                            "parent_fid": "0",
                            "filename": "movie.mkv",
                            "file_type": "1",
                            "size": 1024,
                        }
                    ],
                    "last_page": True,
                    "next_query_cursor": {},
                },
            },
        )

    provider, client = make_provider(handler)
    try:
        first = await provider.list_target_items(FolderRef("0", "/"))
        second = await provider.list_target_items(FolderRef("0", "/"), first.next_marker)
    finally:
        await client.aclose()

    assert first.items[0].item_type == "folder"
    assert first.items[0].filename == "Media"
    assert first.next_marker is not None
    assert second.items[0].item_type == "file"
    assert second.items[0].size == 1024
    assert second.next_marker is None


async def test_resolve_and_create_folder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.oplist.test":
            return httpx.Response(
                200,
                json={"access_token": "access", "refresh_token": "old-refresh-token"},
            )
        if request.url.path == "/open/v1/file/list":
            body = json.loads(request.content)
            if body["parent_fid"] == "0":
                return httpx.Response(
                    200,
                    json={
                        "status": 0,
                        "data": {
                            "file_list": [
                                {
                                    "fid": "media",
                                    "parent_fid": "0",
                                    "filename": "Media",
                                    "file_type": "0",
                                }
                            ],
                            "last_page": True,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={"status": 0, "data": {"file_list": [], "last_page": True}},
            )
        assert request.url.path == "/open/v1/dir"
        assert json.loads(request.content) == {"dir_path": "Movies", "pdir_fid": "media"}
        return httpx.Response(200, json={"status": 0, "data": {"fid": "movies"}})

    provider, client = make_provider(handler)
    try:
        media = await provider.resolve_target_path("/Media")
        movies = await provider.ensure_folder(media, "Movies")
    finally:
        await client.aclose()

    assert media == FolderRef("media", "/Media")
    assert movies == FolderRef("movies", "/Media/Movies")


async def test_share_capabilities_are_explicitly_private_only() -> None:
    provider, client = make_provider(lambda _: httpx.Response(500))
    source = RemoteItem("fid", "0", "file", "file")
    try:
        with pytest.raises(ProviderCapabilityError, match="private provider"):
            await provider.resolve_share("https://pan.quark.cn/s/share")
        with pytest.raises(ProviderCapabilityError, match="private provider"):
            await provider.save_shared_item(
                ShareInfo("share", "share"), source, FolderRef("0", "/")
            )
    finally:
        await client.aclose()


def test_invalid_marker_is_rejected_without_network() -> None:
    provider, _ = make_provider(lambda _: httpx.Response(500))
    with pytest.raises(ValueError, match="page marker"):
        provider._decode_marker("not-json")


def test_open_cli_redacts_input_secrets(monkeypatch, capsys) -> None:
    secrets = iter(["refresh-secret", "app-secret", "sign-secret"])
    monkeypatch.setattr(open_cli.getpass, "getpass", lambda _prompt: next(secrets))

    async def fail(*_args, **_kwargs):
        raise ProviderRequestError(
            "failed refresh-secret app-secret sign-secret"
        )

    monkeypatch.setattr(open_cli, "_run", fail)

    assert open_cli.main([]) == 1
    output = capsys.readouterr().out
    assert "[redacted]" in output
    assert "refresh-secret" not in output
    assert "app-secret" not in output
    assert "sign-secret" not in output
