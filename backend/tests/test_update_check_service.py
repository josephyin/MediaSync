import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.services.update_check_service import (
    GITHUB_RELEASES_URL,
    GitHubReleaseClient,
    UpdateCheckError,
    UpdateCheckService,
    channel_for_version,
    parse_version,
)

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


def release(
    tag: str,
    *,
    prerelease: bool,
    draft: bool = False,
    published_at: str = "2026-07-31T00:00:00Z",
) -> dict[str, Any]:
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": draft,
        "published_at": published_at,
        "html_url": f"https://github.com/josephyin/MediaSync/releases/tag/{tag}",
        "body": f"{tag} 更新说明",
    }


def service(
    fetcher,
    *,
    current_version: str = "0.2.0-rc.9",
    monotonic_value: list[float] | None = None,
) -> UpdateCheckService:
    values = monotonic_value or [100.0]
    return UpdateCheckService(
        current_version=current_version,
        fetch_releases=fetcher,
        cache_seconds=3600,
        failure_retry_seconds=60,
        clock=lambda: NOW,
        monotonic_clock=lambda: values[0],
    )


def test_version_parser_and_channel_are_deterministic() -> None:
    assert parse_version("v0.2.0-rc.9") < parse_version("0.2.0")  # type: ignore[operator]
    assert parse_version("v0.2.1-rc.1") > parse_version("0.2.0")  # type: ignore[operator]
    assert parse_version("latest") is None
    assert channel_for_version("0.2.0-rc.9") == "rc"
    assert channel_for_version("0.2.0") == "stable"


@pytest.mark.asyncio
async def test_stable_channel_ignores_prereleases() -> None:
    async def fetcher() -> list[dict[str, Any]]:
        return [
            release("v0.3.0-rc.1", prerelease=True),
            release("v0.2.1", prerelease=False),
        ]

    result = await service(fetcher, current_version="0.2.0").check()

    assert result.status == "update_available"
    assert result.channel == "stable"
    assert result.latest_release is not None
    assert result.latest_release.version == "0.2.1"
    assert result.manual_upgrade.image == "josephyjq/mediasync:v0.2.1"


@pytest.mark.asyncio
async def test_rc_channel_selects_highest_stable_or_prerelease() -> None:
    async def fetcher() -> list[dict[str, Any]]:
        return [
            release("v0.3.0-rc.2", prerelease=True),
            release("v0.3.0-rc.4", prerelease=True),
            release("v0.2.9", prerelease=False),
            release("v9.0.0", prerelease=False, draft=True),
        ]

    result = await service(fetcher).check()

    assert result.status == "update_available"
    assert result.latest_release is not None
    assert result.latest_release.version == "0.3.0-rc.4"


@pytest.mark.asyncio
async def test_successful_result_is_cached() -> None:
    calls = 0
    monotonic = [100.0]

    async def fetcher() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return [release("v0.2.0-rc.9", prerelease=True)]

    checker = service(fetcher, monotonic_value=monotonic)
    first = await checker.check()
    monotonic[0] = 200.0
    second = await checker.check()

    assert first.status == "current"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_successful_result_as_stale() -> None:
    monotonic = [100.0]
    responses: list[object] = [
        [release("v0.3.0-rc.1", prerelease=True)],
        UpdateCheckError("无法连接 GitHub，请稍后重试"),
    ]

    async def fetcher() -> list[dict[str, Any]]:
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]

    checker = service(fetcher, monotonic_value=monotonic)
    first = await checker.check()
    monotonic[0] = 4000.0
    second = await checker.check()

    assert first.stale is False
    assert second.status == "update_available"
    assert second.stale is True
    assert second.error_message == "无法连接 GitHub，请稍后重试"
    assert second.latest_release == first.latest_release


@pytest.mark.asyncio
async def test_failed_result_is_rate_limited_without_faking_current_status() -> None:
    calls = 0

    async def fetcher() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        raise UpdateCheckError("GitHub 请求受限，请稍后重试")

    checker = service(fetcher)
    first = await checker.check()
    second = await checker.check()

    assert first.status == "error"
    assert first.latest_release is None
    assert second.status == "error"
    assert second.cache_hit is True
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_release_payload_does_not_claim_current_version() -> None:
    async def fetcher() -> list[dict[str, Any]]:
        return [
            release("nightly", prerelease=False),
            {"tag_name": "v0.3.0", "prerelease": False},
        ]

    result = await service(fetcher).check()

    assert result.status == "error"
    assert result.error_message == "未找到适用于当前更新频道的官方版本"


@pytest.mark.asyncio
async def test_concurrent_checks_share_one_external_request() -> None:
    calls = 0
    gate = asyncio.Event()

    async def fetcher() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        await gate.wait()
        return [release("v0.3.0-rc.1", prerelease=True)]

    checker = service(fetcher)
    first = asyncio.create_task(checker.check())
    second = asyncio.create_task(checker.check())
    await asyncio.sleep(0)
    gate.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls == 1
    assert first_result.cache_hit is False
    assert second_result.cache_hit is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(403, json={"message": "rate limit"}), "GitHub 请求受限，请稍后重试"),
        (httpx.Response(502, text="bad gateway"), "GitHub 版本服务暂时不可用"),
        (httpx.Response(200, text="not-json"), "GitHub 返回了无法解析的版本信息"),
        (httpx.Response(200, json={"tag_name": "v0.3.0"}), "GitHub 返回了无效的版本信息"),
    ],
)
async def test_github_client_maps_remote_failures_to_safe_messages(
    response: httpx.Response,
    message: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(GITHUB_RELEASES_URL)
        return response

    client = GitHubReleaseClient(
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpdateCheckError, match=message):
        await client.fetch_releases()


@pytest.mark.asyncio
async def test_github_client_maps_timeout_to_safe_message() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    client = GitHubReleaseClient(
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(UpdateCheckError, match="无法连接 GitHub，请稍后重试"):
        await client.fetch_releases()


def test_status_before_first_check_is_explicit() -> None:
    async def fetcher() -> list[dict[str, Any]]:
        return []

    result = service(fetcher).get_status()

    assert result.status == "not_checked"
    assert result.checked_at is None
    assert result.error_message is None
    assert result.manual_upgrade.image == "josephyjq/mediasync:rc"


def test_notes_are_bounded() -> None:
    long_notes = "a" * 5000

    async def fetcher() -> list[dict[str, Any]]:
        return []

    checker = service(fetcher)
    parsed = checker._parse_release(
        {
            **release("v0.3.0-rc.1", prerelease=True),
            "body": long_notes,
            "published_at": (NOW + timedelta(days=1)).isoformat(),
        }
    )

    assert parsed is not None
    assert len(parsed.info.notes) == 4000
