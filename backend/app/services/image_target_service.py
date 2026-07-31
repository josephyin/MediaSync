from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.services.docker_capability_service import (
    APPLIANCE_COMMAND,
    OFFICIAL_SOURCE,
    DockerEngineClient,
    DockerEngineError,
)

MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
VERSION_PATTERN = re.compile(
    r"^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-rc\.(?:0|[1-9]\d*))?$"
)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
AUTH_PARAMETER_PATTERN = re.compile(r'([A-Za-z]+)="([^"]*)"')


class ImageTargetError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfficialRegistry:
    key: str
    repository: str
    registry_host: str
    registry_repository: str
    auth_hosts: frozenset[str]

    @property
    def manifest_base_url(self) -> str:
        return f"https://{self.registry_host}/v2/{self.registry_repository}/manifests"


OFFICIAL_REGISTRIES = {
    "dockerhub": OfficialRegistry(
        key="dockerhub",
        repository="josephyjq/mediasync",
        registry_host="registry-1.docker.io",
        registry_repository="josephyjq/mediasync",
        auth_hosts=frozenset({"auth.docker.io"}),
    ),
    "ghcr": OfficialRegistry(
        key="ghcr",
        repository="ghcr.io/josephyin/mediasync",
        registry_host="ghcr.io",
        registry_repository="josephyin/mediasync",
        auth_hosts=frozenset({"ghcr.io"}),
    ),
}


@dataclass(frozen=True)
class ResolvedImageTarget:
    registry: str
    repository: str
    version: str
    digest: str

    @property
    def immutable_reference(self) -> str:
        return f"{self.repository}@{self.digest}"


@dataclass(frozen=True)
class VerifiedImageTarget(ResolvedImageTarget):
    revision: str


class ManifestResolver(Protocol):
    async def resolve(
        self,
        *,
        registry_key: str,
        version: str,
    ) -> ResolvedImageTarget: ...


class ImageEngine(Protocol):
    async def inspect_image(self, reference: str) -> dict[str, Any] | None: ...

    async def pull_image(
        self,
        reference: str,
        *,
        timeout_seconds: float,
    ) -> None: ...


class RegistryManifestResolver:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def resolve(
        self,
        *,
        registry_key: str,
        version: str,
    ) -> ResolvedImageTarget:
        normalized_version = validate_exact_version(version)
        registry = get_official_registry(registry_key)
        manifest_url = f"{registry.manifest_base_url}/{normalized_version}"
        headers = {"Accept": MANIFEST_ACCEPT}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(manifest_url, headers=headers)
                if response.status_code == 401:
                    token = await self._request_token(
                        client,
                        registry=registry,
                        challenge=response.headers.get("WWW-Authenticate", ""),
                    )
                    response = await client.get(
                        manifest_url,
                        headers={**headers, "Authorization": f"Bearer {token}"},
                    )
        except httpx.HTTPError as exc:
            raise ImageTargetError("官方镜像仓库不可访问") from exc

        if response.status_code != 200:
            raise ImageTargetError("无法解析目标版本的官方镜像")
        digest = normalize_digest(response.headers.get("Docker-Content-Digest", ""))
        return ResolvedImageTarget(
            registry=registry.key,
            repository=registry.repository,
            version=normalized_version,
            digest=digest,
        )

    async def _request_token(
        self,
        client: httpx.AsyncClient,
        *,
        registry: OfficialRegistry,
        challenge: str,
    ) -> str:
        scheme, separator, parameters = challenge.partition(" ")
        if separator != " " or scheme.lower() != "bearer":
            raise ImageTargetError("官方镜像仓库认证挑战无效")
        values = dict(AUTH_PARAMETER_PATTERN.findall(parameters))
        realm = values.get("realm", "")
        parsed_realm = urlparse(realm)
        if (
            parsed_realm.scheme != "https"
            or parsed_realm.hostname not in registry.auth_hosts
            or parsed_realm.username is not None
            or parsed_realm.password is not None
        ):
            raise ImageTargetError("官方镜像仓库认证地址无效")
        service = values.get("service")
        scope = values.get("scope")
        if not service or scope != f"repository:{registry.registry_repository}:pull":
            raise ImageTargetError("官方镜像仓库认证范围无效")
        response = await client.get(
            realm,
            params={"service": service, "scope": scope},
        )
        if response.status_code != 200:
            raise ImageTargetError("官方镜像仓库认证失败")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ImageTargetError("官方镜像仓库返回无效认证结果") from exc
        if not isinstance(payload, dict):
            raise ImageTargetError("官方镜像仓库返回无效认证结果")
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ImageTargetError("官方镜像仓库返回无效认证结果")
        return token


class ImageTargetService:
    def __init__(
        self,
        *,
        resolver: ManifestResolver,
        engine: ImageEngine,
        pull_timeout_seconds: float,
    ) -> None:
        self.resolver = resolver
        self.engine = engine
        self.pull_timeout_seconds = pull_timeout_seconds

    async def pull_and_verify(
        self,
        *,
        registry_key: str,
        version: str,
    ) -> VerifiedImageTarget:
        target = await self.resolver.resolve(
            registry_key=registry_key,
            version=version,
        )
        validate_resolved_target(target, registry_key=registry_key, version=version)

        try:
            existing = await self.engine.inspect_image(
                f"{target.repository}:{target.version}"
            )
        except DockerEngineError as exc:
            raise ImageTargetError("无法检查本地目标版本镜像") from exc
        if existing is not None:
            reject_same_version_digest_drift(existing, target)

        try:
            await self.engine.pull_image(
                target.immutable_reference,
                timeout_seconds=self.pull_timeout_seconds,
            )
            image = await self.engine.inspect_image(target.immutable_reference)
        except DockerEngineError as exc:
            raise ImageTargetError("目标镜像拉取或读取失败") from exc
        if image is None:
            raise ImageTargetError("拉取后未找到目标镜像")
        revision = validate_pulled_image(image, target)
        return VerifiedImageTarget(
            registry=target.registry,
            repository=target.repository,
            version=target.version,
            digest=target.digest,
            revision=revision,
        )


def get_official_registry(registry_key: str) -> OfficialRegistry:
    try:
        return OFFICIAL_REGISTRIES[registry_key]
    except KeyError as exc:
        raise ImageTargetError("不支持的镜像仓库") from exc


def validate_exact_version(version: str) -> str:
    normalized = version.strip()
    if not VERSION_PATTERN.fullmatch(normalized):
        raise ImageTargetError("更新目标必须使用精确版本")
    return normalized


def normalize_digest(digest: str) -> str:
    normalized = digest.strip().lower()
    if not DIGEST_PATTERN.fullmatch(normalized):
        raise ImageTargetError("官方镜像仓库返回无效 digest")
    return normalized


def validate_resolved_target(
    target: ResolvedImageTarget,
    *,
    registry_key: str,
    version: str,
) -> None:
    registry = get_official_registry(registry_key)
    expected_version = validate_exact_version(version)
    if (
        target.registry != registry.key
        or target.repository != registry.repository
        or target.version != expected_version
    ):
        raise ImageTargetError("镜像解析结果不属于允许的官方目标")
    normalize_digest(target.digest)


def reject_same_version_digest_drift(
    image: dict[str, Any],
    target: ResolvedImageTarget,
) -> None:
    labels = image_labels(image)
    if normalize_label_version(labels.get("org.opencontainers.image.version")) != (
        normalize_label_version(target.version)
    ):
        return
    repo_digests = image.get("RepoDigests")
    if not isinstance(repo_digests, list):
        raise ImageTargetError("本地同版本镜像缺少 digest")
    if not repo_digests_contain_target(repo_digests, target):
        raise ImageTargetError("检测到相同版本的镜像 digest 已变化")


def validate_pulled_image(
    image: dict[str, Any],
    target: ResolvedImageTarget,
) -> str:
    repo_digests = image.get("RepoDigests")
    if not isinstance(repo_digests, list) or not repo_digests_contain_target(
        repo_digests, target
    ):
        raise ImageTargetError("目标镜像 digest 校验失败")
    config = image.get("Config")
    if not isinstance(config, dict):
        raise ImageTargetError("目标镜像配置不完整")
    if config.get("Cmd") != APPLIANCE_COMMAND:
        raise ImageTargetError("目标镜像不是 Appliance 运行模式")
    labels = image_labels(image)
    if (
        labels.get("org.opencontainers.image.source") != OFFICIAL_SOURCE
        or labels.get("org.opencontainers.image.title") != "MediaSync"
    ):
        raise ImageTargetError("目标镜像来源校验失败")
    if normalize_label_version(labels.get("org.opencontainers.image.version")) != (
        normalize_label_version(target.version)
    ):
        raise ImageTargetError("目标镜像版本校验失败")
    revision = labels.get("org.opencontainers.image.revision")
    if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
        raise ImageTargetError("目标镜像源码修订校验失败")
    return revision


def image_labels(image: dict[str, Any]) -> dict[str, Any]:
    config = image.get("Config")
    if not isinstance(config, dict):
        return {}
    labels = config.get("Labels")
    return labels if isinstance(labels, dict) else {}


def repo_digests_contain_target(
    repo_digests: list[object],
    target: ResolvedImageTarget,
) -> bool:
    repositories = {target.repository}
    if target.registry == "dockerhub":
        repositories.add(f"docker.io/{target.repository}")
    expected = {f"{repository}@{target.digest}" for repository in repositories}
    return any(item in expected for item in repo_digests if isinstance(item, str))


def normalize_label_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.removeprefix("v")


def get_image_target_service() -> ImageTargetService:
    from app.core.config import get_settings

    settings = get_settings()
    return ImageTargetService(
        resolver=RegistryManifestResolver(
            timeout_seconds=settings.update_registry_timeout_seconds
        ),
        engine=DockerEngineClient(
            socket_path=settings.docker_socket_path,
            timeout_seconds=settings.docker_api_timeout_seconds,
        ),
        pull_timeout_seconds=settings.docker_image_pull_timeout_seconds,
    )
