from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import httpx

from app.core.config import get_settings
from app.schemas.update import (
    DockerCapabilityInfo,
    ManualUpgradeInfo,
    ReleaseInfo,
    UpdateChannel,
    UpdateStatusRead,
)

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/josephyin/MediaSync/releases"
OFFICIAL_DOCKER_IMAGE = "josephyjq/mediasync"
RELEASE_NOTES_LIMIT = 4000
VERSION_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-rc\.(?P<rc>0|[1-9]\d*))?$"
)

ReleaseFetcher = Callable[[], Awaitable[list[dict[str, Any]]]]
Clock = Callable[[], datetime]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True, order=True)
class ParsedVersion:
    major: int
    minor: int
    patch: int
    stability: int
    prerelease_number: int


@dataclass(frozen=True)
class ParsedRelease:
    version: ParsedVersion
    info: ReleaseInfo


class UpdateCheckError(RuntimeError):
    pass


def parse_version(value: str) -> ParsedVersion | None:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    rc_value = match.group("rc")
    return ParsedVersion(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        stability=0 if rc_value is not None else 1,
        prerelease_number=int(rc_value or 0),
    )


def channel_for_version(value: str) -> UpdateChannel:
    parsed = parse_version(value)
    return "rc" if parsed is not None and parsed.stability == 0 else "stable"


class GitHubReleaseClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def fetch_releases(self) -> list[dict[str, Any]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "MediaSync-Update-Checker",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                headers=headers,
                transport=self.transport,
            ) as client:
                response = await client.get(GITHUB_RELEASES_URL, params={"per_page": 20})
        except httpx.HTTPError as exc:
            raise UpdateCheckError("无法连接 GitHub，请稍后重试") from exc

        if response.status_code in {403, 429}:
            raise UpdateCheckError("GitHub 请求受限，请稍后重试")
        if response.status_code != 200:
            raise UpdateCheckError("GitHub 版本服务暂时不可用")

        try:
            payload = response.json()
        except ValueError as exc:
            raise UpdateCheckError("GitHub 返回了无法解析的版本信息") from exc
        if not isinstance(payload, list):
            raise UpdateCheckError("GitHub 返回了无效的版本信息")
        return [item for item in payload if isinstance(item, dict)]


class UpdateCheckService:
    def __init__(
        self,
        *,
        current_version: str,
        fetch_releases: ReleaseFetcher,
        cache_seconds: int,
        failure_retry_seconds: int,
        clock: Clock | None = None,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self.current_version = current_version
        self.channel = channel_for_version(current_version)
        self.fetch_releases = fetch_releases
        self.cache_seconds = cache_seconds
        self.failure_retry_seconds = failure_retry_seconds
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic_clock = monotonic_clock or time.monotonic
        self._lock = asyncio.Lock()
        self._latest_release: ReleaseInfo | None = None
        self._last_checked_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_attempt_monotonic: float | None = None
        self._last_attempt_succeeded = False
        self._last_error: str | None = None

    def get_status(self, *, cache_hit: bool = False) -> UpdateStatusRead:
        parsed_current = parse_version(self.current_version)
        parsed_latest = (
            parse_version(self._latest_release.version) if self._latest_release else None
        )
        if self._last_error is not None and self._latest_release is None:
            status = "error"
        elif self._latest_release is None or parsed_current is None or parsed_latest is None:
            status = "not_checked"
        elif parsed_latest > parsed_current:
            status = "update_available"
        else:
            status = "current"

        return UpdateStatusRead(
            current_version=self.current_version,
            channel=self.channel,
            status=status,
            install_unavailable_reason=(
                "当前版本仅提供检查更新；请在 NAS 容器管理器中保留 /data 并更新镜像"
            ),
            docker_capability=DockerCapabilityInfo(
                reason_code="not_probed",
                message="尚未探测 Docker 更新能力",
            ),
            latest_release=self._latest_release,
            checked_at=self._last_checked_at,
            last_success_at=self._last_success_at,
            stale=self._last_error is not None and self._latest_release is not None,
            cache_hit=cache_hit,
            error_message=self._last_error,
            manual_upgrade=ManualUpgradeInfo(
                image=self._manual_image(),
                message="拉取新镜像后，使用原端口、环境变量和 /data 映射重建容器",
            ),
        )

    async def check(self) -> UpdateStatusRead:
        async with self._lock:
            now_monotonic = self.monotonic_clock()
            if self._should_use_cached_result(now_monotonic):
                return self.get_status(cache_hit=True)

            self._last_attempt_monotonic = now_monotonic
            self._last_checked_at = self.clock()
            try:
                releases = await self.fetch_releases()
                latest = self._select_latest(releases)
                if latest is None:
                    raise UpdateCheckError("未找到适用于当前更新频道的官方版本")
            except UpdateCheckError as exc:
                self._last_attempt_succeeded = False
                self._last_error = str(exc)
                logger.warning(
                    "update_check_failed channel=%s has_cached_release=%s reason=%s",
                    self.channel,
                    self._latest_release is not None,
                    type(exc).__name__,
                )
                return self.get_status()
            except Exception:
                self._last_attempt_succeeded = False
                self._last_error = "检查更新时发生未知错误，请稍后重试"
                logger.exception(
                    "update_check_unexpected_error channel=%s has_cached_release=%s",
                    self.channel,
                    self._latest_release is not None,
                )
                return self.get_status()

            self._latest_release = latest.info
            self._last_success_at = self._last_checked_at
            self._last_attempt_succeeded = True
            self._last_error = None
            logger.info(
                "update_check_completed channel=%s current_version=%s latest_version=%s",
                self.channel,
                self.current_version,
                latest.info.version,
            )
            return self.get_status()

    def _should_use_cached_result(self, now_monotonic: float) -> bool:
        if self._last_attempt_monotonic is None:
            return False
        elapsed = now_monotonic - self._last_attempt_monotonic
        ttl = self.cache_seconds if self._last_attempt_succeeded else self.failure_retry_seconds
        return elapsed < ttl

    def _select_latest(self, releases: list[dict[str, Any]]) -> ParsedRelease | None:
        candidates: list[ParsedRelease] = []
        for release in releases:
            if release.get("draft") is True:
                continue
            parsed = self._parse_release(release)
            if parsed is None:
                continue
            if self.channel == "stable" and parsed.info.prerelease:
                continue
            candidates.append(parsed)
        return max(candidates, key=lambda item: item.version) if candidates else None

    def _parse_release(self, release: dict[str, Any]) -> ParsedRelease | None:
        tag_name = release.get("tag_name")
        published_at = release.get("published_at")
        release_url = release.get("html_url")
        release_fields = (tag_name, published_at, release_url)
        if not all(isinstance(value, str) and value for value in release_fields):
            return None
        version = parse_version(tag_name)
        if version is None:
            return None
        try:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        prerelease = bool(release.get("prerelease"))
        if prerelease != (version.stability == 0):
            return None
        notes = release.get("body")
        return ParsedRelease(
            version=version,
            info=ReleaseInfo(
                version=tag_name.removeprefix("v"),
                tag_name=tag_name,
                published_at=published,
                release_url=release_url,
                notes=notes[:RELEASE_NOTES_LIMIT] if isinstance(notes, str) else "",
                prerelease=prerelease,
            ),
        )

    def _manual_image(self) -> str:
        if self._latest_release is None:
            return f"{OFFICIAL_DOCKER_IMAGE}:{'rc' if self.channel == 'rc' else 'latest'}"
        return f"{OFFICIAL_DOCKER_IMAGE}:{self._latest_release.tag_name}"


@lru_cache
def get_update_check_service() -> UpdateCheckService:
    settings = get_settings()
    client = GitHubReleaseClient(timeout_seconds=settings.update_check_timeout_seconds)
    return UpdateCheckService(
        current_version=settings.app_version,
        fetch_releases=client.fetch_releases,
        cache_seconds=settings.update_check_cache_seconds,
        failure_retry_seconds=settings.update_check_failure_retry_seconds,
    )
