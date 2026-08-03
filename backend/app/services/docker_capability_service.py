from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.schemas.update import DockerCapabilityInfo

logger = logging.getLogger(__name__)

OFFICIAL_SOURCE = "https://github.com/josephyin/MediaSync"
APPLIANCE_COMMAND = ["python", "-m", "app.appliance"]
CONTAINER_ID_PATTERN = re.compile(r"^[0-9a-f]{12,64}$")
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


class DockerEngineError(RuntimeError):
    pass


class DockerEngine(Protocol):
    async def ping(self) -> None: ...

    async def inspect_container(self, container_id: str) -> dict[str, Any]: ...

    async def list_containers(self) -> list[dict[str, Any]]: ...


class DockerEngineClient:
    def __init__(
        self,
        *,
        socket_path: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        transport = self.transport or httpx.AsyncHTTPTransport(uds=self.socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://docker",
                timeout=timeout_seconds or self.timeout_seconds,
            ) as client:
                return await client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise DockerEngineError("Docker Engine API 不可访问") from exc

    async def _get(self, path: str) -> httpx.Response:
        return await self._request("GET", path)

    async def ping(self) -> None:
        response = await self._get("/_ping")
        if response.status_code != 200 or response.text.strip() != "OK":
            raise DockerEngineError("Docker Engine API 响应异常")

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        if not CONTAINER_ID_PATTERN.fullmatch(container_id):
            raise DockerEngineError("容器标识格式无效")
        response = await self._get(f"/containers/{container_id}/json")
        if response.status_code != 200:
            raise DockerEngineError("无法读取当前容器信息")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DockerEngineError("Docker Engine 返回无效容器信息") from exc
        if not isinstance(payload, dict):
            raise DockerEngineError("Docker Engine 返回无效容器信息")
        return payload

    async def list_containers(self) -> list[dict[str, Any]]:
        response = await self._get("/containers/json?all=1")
        if response.status_code != 200:
            raise DockerEngineError("无法查询容器列表")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DockerEngineError("Docker Engine 返回无效容器列表") from exc
        if not isinstance(payload, list):
            raise DockerEngineError("Docker Engine 返回无效容器列表")
        return [item for item in payload if isinstance(item, dict)]

    async def inspect_image(self, reference: str) -> dict[str, Any] | None:
        response = await self._get(f"/images/{quote(reference, safe='')}/json")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise DockerEngineError("无法读取目标镜像信息")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DockerEngineError("Docker Engine 返回无效镜像信息") from exc
        if not isinstance(payload, dict):
            raise DockerEngineError("Docker Engine 返回无效镜像信息")
        return payload

    async def pull_image(
        self,
        reference: str,
        *,
        timeout_seconds: float,
    ) -> None:
        response = await self._request(
            "POST",
            "/images/create",
            params={"fromImage": reference},
            timeout_seconds=timeout_seconds,
        )
        if response.status_code != 200:
            raise DockerEngineError("目标镜像拉取失败")
        for line in response.text.splitlines():
            if not line.strip():
                continue
            try:
                event = httpx.Response(200, content=line).json()
            except ValueError as exc:
                raise DockerEngineError("Docker Engine 返回无效拉取结果") from exc
            if not isinstance(event, dict):
                raise DockerEngineError("Docker Engine 返回无效拉取结果")
            if event.get("error") or event.get("errorDetail"):
                raise DockerEngineError("目标镜像拉取失败")

    async def create_container(
        self,
        *,
        name: str,
        config: dict[str, Any],
    ) -> str:
        response = await self._request(
            "POST",
            "/containers/create",
            params={"name": name},
            json=config,
        )
        if response.status_code != 201:
            raise DockerEngineError("无法创建 updater 助手容器")
        try:
            payload = response.json()
        except ValueError as exc:
            raise DockerEngineError("Docker Engine 返回无效创建结果") from exc
        container_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(container_id, str) or not CONTAINER_ID_PATTERN.fullmatch(
            container_id
        ):
            raise DockerEngineError("Docker Engine 返回无效容器标识")
        return container_id


SocketProbe = Callable[[str], str]


def probe_socket_type(path: str) -> str:
    try:
        mode = Path(path).stat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "inaccessible"
    return "socket" if stat.S_ISSOCK(mode) else "invalid_type"


class DockerCapabilityService:
    def __init__(
        self,
        *,
        socket_path: str,
        explicit_container_id: str,
        hostname: str,
        engine: DockerEngine,
        cache_seconds: int,
        socket_probe: SocketProbe = probe_socket_type,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.socket_path = socket_path
        self.explicit_container_id = explicit_container_id.strip().lower()
        self.hostname = hostname.strip().lower()
        self.engine = engine
        self.cache_seconds = cache_seconds
        self.socket_probe = socket_probe
        self.monotonic_clock = monotonic_clock
        self._lock = asyncio.Lock()
        self._cached: DockerCapabilityInfo | None = None
        self._cached_at: float | None = None

    async def probe(self) -> DockerCapabilityInfo:
        async with self._lock:
            now = self.monotonic_clock()
            if (
                self._cached is not None
                and self._cached_at is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return self._cached
            result = await self._probe_uncached()
            self._cached = result
            self._cached_at = now
            return result

    async def _probe_uncached(self) -> DockerCapabilityInfo:
        socket_state = self.socket_probe(self.socket_path)
        if socket_state == "missing":
            return self._result("socket_missing", "未挂载 Docker Socket")
        if socket_state == "invalid_type":
            return self._result("socket_invalid", "Docker Socket 挂载类型无效")
        if socket_state != "socket":
            return self._result("socket_inaccessible", "Docker Socket 不可访问")

        try:
            await self.engine.ping()
        except DockerEngineError:
            logger.warning("docker_capability_probe_failed reason=engine_unavailable")
            return self._result(
                "engine_unavailable",
                "Docker Engine API 不可访问或权限不足",
                socket_available=True,
            )

        try:
            container = await self._identify_container()
        except DockerEngineError as exc:
            logger.warning(
                "docker_capability_probe_failed reason=container_not_identified"
            )
            return self._result(
                "container_not_identified",
                str(exc),
                socket_available=True,
                engine_available=True,
            )

        rejection = validate_current_container(container)
        if rejection is not None:
            reason_code, message = rejection
            logger.warning("docker_capability_probe_failed reason=%s", reason_code)
            return self._result(
                reason_code,
                message,
                socket_available=True,
                engine_available=True,
            )

        return self._result(
            "ready",
            "Docker 环境与当前 MediaSync 容器已安全识别",
            socket_available=True,
            engine_available=True,
            container_identified=True,
        )

    async def _identify_container(self) -> dict[str, Any]:
        if self.explicit_container_id:
            return await self.engine.inspect_container(self.explicit_container_id)

        if CONTAINER_ID_PATTERN.fullmatch(self.hostname):
            try:
                return await self.engine.inspect_container(self.hostname)
            except DockerEngineError:
                pass

        summaries = await self.engine.list_containers()
        candidates = [
            item
            for item in summaries
            if has_official_source(item.get("Labels"))
            and summary_has_data_mount(item)
            and item.get("Command") == "python -m app.appliance"
        ]
        if len(candidates) != 1:
            raise DockerEngineError(
                "无法唯一识别当前 MediaSync Appliance 容器"
            )
        container_id = candidates[0].get("Id")
        if not isinstance(container_id, str) or not CONTAINER_ID_PATTERN.fullmatch(
            container_id
        ):
            raise DockerEngineError("候选容器标识无效")
        return await self.engine.inspect_container(container_id)

    @staticmethod
    def _result(
        reason_code: str,
        message: str,
        *,
        socket_available: bool = False,
        engine_available: bool = False,
        container_identified: bool = False,
    ) -> DockerCapabilityInfo:
        return DockerCapabilityInfo(
            socket_available=socket_available,
            engine_available=engine_available,
            container_identified=container_identified,
            reason_code=reason_code,
            message=message,
        )


def has_official_source(labels: object) -> bool:
    return (
        isinstance(labels, dict)
        and labels.get("org.opencontainers.image.source") == OFFICIAL_SOURCE
        and labels.get("org.opencontainers.image.title") == "MediaSync"
    )


def summary_has_data_mount(summary: dict[str, Any]) -> bool:
    mounts = summary.get("Mounts")
    return isinstance(mounts, list) and any(
        isinstance(mount, dict)
        and mount.get("Destination") == "/data"
        and mount.get("Type") in {"bind", "volume"}
        for mount in mounts
    )


def validate_current_container(
    container: dict[str, Any],
) -> tuple[str, str] | None:
    config = container.get("Config")
    if not isinstance(config, dict):
        return ("container_invalid", "当前容器信息不完整")
    labels = config.get("Labels")
    if not has_official_source(labels):
        return ("container_unofficial", "当前容器不是 MediaSync 官方镜像")
    if isinstance(labels, dict) and COMPOSE_PROJECT_LABEL in labels:
        return ("compose_managed", "当前容器由 Docker Compose 管理，不支持一键更新")
    if config.get("Cmd") != APPLIANCE_COMMAND:
        return ("not_appliance", "当前容器不是默认 Appliance 运行模式")
    if not summary_has_data_mount(container):
        return ("data_mount_missing", "当前容器未持久化挂载 /data")
    return None


@lru_cache
def get_docker_capability_service() -> DockerCapabilityService:
    settings = get_settings()
    return DockerCapabilityService(
        socket_path=settings.docker_socket_path,
        explicit_container_id=settings.docker_container_id,
        hostname=os.environ.get("HOSTNAME", ""),
        engine=DockerEngineClient(
            socket_path=settings.docker_socket_path,
            timeout_seconds=settings.docker_api_timeout_seconds,
        ),
        cache_seconds=settings.docker_capability_cache_seconds,
    )
