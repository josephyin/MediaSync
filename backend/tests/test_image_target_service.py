from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.services.docker_capability_service import (
    APPLIANCE_COMMAND,
    OFFICIAL_SOURCE,
    DockerEngineClient,
)
from app.services.image_target_service import (
    ImageTargetError,
    ImageTargetService,
    RegistryManifestResolver,
    ResolvedImageTarget,
)

DIGEST = f"sha256:{'a' * 64}"
OTHER_DIGEST = f"sha256:{'b' * 64}"
REVISION = "c" * 40
VERSION = "v0.3.0-rc.1"
DOCKERHUB_REPOSITORY = "josephyjq/mediasync"


def official_image(
    *,
    digest: str = DIGEST,
    version: str = VERSION,
    source: str = OFFICIAL_SOURCE,
    revision: str = REVISION,
) -> dict[str, Any]:
    return {
        "RepoDigests": [f"{DOCKERHUB_REPOSITORY}@{digest}"],
        "Config": {
            "Cmd": APPLIANCE_COMMAND,
            "Labels": {
                "org.opencontainers.image.source": source,
                "org.opencontainers.image.title": "MediaSync",
                "org.opencontainers.image.version": version,
                "org.opencontainers.image.revision": revision,
            },
        },
    }


class FakeResolver:
    def __init__(self, target: ResolvedImageTarget | None = None) -> None:
        self.target = target or ResolvedImageTarget(
            registry="dockerhub",
            repository=DOCKERHUB_REPOSITORY,
            version=VERSION,
            digest=DIGEST,
        )
        self.calls: list[tuple[str, str]] = []

    async def resolve(
        self,
        *,
        registry_key: str,
        version: str,
    ) -> ResolvedImageTarget:
        self.calls.append((registry_key, version))
        return self.target


class FakeImageEngine:
    def __init__(
        self,
        *,
        tagged_image: dict[str, Any] | None = None,
        pulled_image: dict[str, Any] | None = None,
    ) -> None:
        self.tagged_image = tagged_image
        self.pulled_image = pulled_image or official_image()
        self.inspect_calls: list[str] = []
        self.pull_calls: list[tuple[str, float]] = []

    async def inspect_image(self, reference: str) -> dict[str, Any] | None:
        self.inspect_calls.append(reference)
        if reference.endswith(f":{VERSION}"):
            return self.tagged_image
        return self.pulled_image

    async def pull_image(
        self,
        reference: str,
        *,
        timeout_seconds: float,
    ) -> None:
        self.pull_calls.append((reference, timeout_seconds))


@pytest.mark.asyncio
async def test_registry_resolver_follows_bounded_bearer_challenge() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if (
            request.url.host == "registry-1.docker.io"
            and "authorization" not in request.headers
        ):
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="https://auth.docker.io/token",'
                        'service="registry.docker.io",'
                        'scope="repository:josephyjq/mediasync:pull"'
                    )
                },
            )
        if request.url.host == "auth.docker.io":
            return httpx.Response(200, json={"token": "registry-token"})
        return httpx.Response(
            200,
            headers={"Docker-Content-Digest": DIGEST},
            json={"schemaVersion": 2},
        )

    resolver = RegistryManifestResolver(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    target = await resolver.resolve(registry_key="dockerhub", version=VERSION)

    assert target.immutable_reference == f"{DOCKERHUB_REPOSITORY}@{DIGEST}"
    assert requests[1].url.params["scope"] == "repository:josephyjq/mediasync:pull"
    assert requests[2].headers["Authorization"] == "Bearer registry-token"


@pytest.mark.asyncio
async def test_registry_resolver_supports_official_ghcr_repository() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "ghcr.io"
        assert request.url.path == (
            f"/v2/josephyin/mediasync/manifests/{VERSION}"
        )
        return httpx.Response(
            200,
            headers={"Docker-Content-Digest": DIGEST},
            json={"schemaVersion": 2},
        )

    resolver = RegistryManifestResolver(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    target = await resolver.resolve(registry_key="ghcr", version=VERSION)

    assert target.repository == "ghcr.io/josephyin/mediasync"
    assert target.immutable_reference == (
        f"ghcr.io/josephyin/mediasync@{DIGEST}"
    )


@pytest.mark.asyncio
async def test_docker_engine_client_uses_encoded_immutable_image_reference() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(200, text='{"status":"Downloaded newer image"}\n')
        return httpx.Response(200, json=official_image())

    client = DockerEngineClient(
        socket_path="/unused-in-test.sock",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    reference = f"{DOCKERHUB_REPOSITORY}@{DIGEST}"

    await client.pull_image(reference, timeout_seconds=600)
    image = await client.inspect_image(reference)

    assert image == official_image()
    assert requests[0].url.params["fromImage"] == reference
    assert requests[1].url.path == f"/images/{reference}/json"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "registry"),
    [
        ("latest", "dockerhub"),
        ("rc", "dockerhub"),
        ("v0.3", "dockerhub"),
        ("0.3.0-rc.1", "dockerhub"),
        ("v0.3.0-beta.1", "dockerhub"),
        (VERSION, "custom"),
    ],
)
async def test_registry_resolver_rejects_floating_version_or_custom_registry(
    version: str,
    registry: str,
) -> None:
    resolver = RegistryManifestResolver(
        timeout_seconds=5,
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )

    with pytest.raises(ImageTargetError):
        await resolver.resolve(registry_key=registry, version=version)


@pytest.mark.asyncio
async def test_registry_resolver_rejects_untrusted_auth_realm() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={
                "WWW-Authenticate": (
                    'Bearer realm="https://evil.example/token",'
                    'service="registry.docker.io",'
                    'scope="repository:josephyjq/mediasync:pull"'
                )
            },
        )

    resolver = RegistryManifestResolver(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ImageTargetError, match="认证地址无效"):
        await resolver.resolve(registry_key="dockerhub", version=VERSION)


@pytest.mark.asyncio
async def test_pull_uses_immutable_reference_and_validates_official_labels() -> None:
    engine = FakeImageEngine()
    service = ImageTargetService(
        resolver=FakeResolver(),
        engine=engine,
        pull_timeout_seconds=600,
    )

    result = await service.pull_and_verify(
        registry_key="dockerhub",
        version=VERSION,
    )

    assert result.digest == DIGEST
    assert result.revision == REVISION
    assert engine.pull_calls == [(f"{DOCKERHUB_REPOSITORY}@{DIGEST}", 600)]
    assert engine.inspect_calls == [
        f"{DOCKERHUB_REPOSITORY}:{VERSION}",
        f"{DOCKERHUB_REPOSITORY}@{DIGEST}",
    ]


@pytest.mark.asyncio
async def test_existing_same_version_with_different_digest_is_rejected_before_pull() -> None:
    engine = FakeImageEngine(
        tagged_image=official_image(digest=OTHER_DIGEST),
    )
    service = ImageTargetService(
        resolver=FakeResolver(),
        engine=engine,
        pull_timeout_seconds=600,
    )

    with pytest.raises(ImageTargetError, match="digest 已变化"):
        await service.pull_and_verify(
            registry_key="dockerhub",
            version=VERSION,
        )

    assert engine.pull_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "message"),
    [
        (official_image(digest=OTHER_DIGEST), "digest 校验失败"),
        (official_image(source="https://example.com/fork"), "来源校验失败"),
        (official_image(version="v0.3.0"), "版本校验失败"),
        (official_image(revision="not-a-commit"), "源码修订校验失败"),
    ],
)
async def test_pulled_image_identity_mismatch_is_rejected(
    image: dict[str, Any],
    message: str,
) -> None:
    service = ImageTargetService(
        resolver=FakeResolver(),
        engine=FakeImageEngine(pulled_image=image),
        pull_timeout_seconds=600,
    )

    with pytest.raises(ImageTargetError, match=message):
        await service.pull_and_verify(
            registry_key="dockerhub",
            version=VERSION,
        )


@pytest.mark.asyncio
async def test_forged_resolver_result_cannot_escape_official_allowlist() -> None:
    resolver = FakeResolver(
        ResolvedImageTarget(
            registry="dockerhub",
            repository="evil.example/mediasync",
            version=VERSION,
            digest=DIGEST,
        )
    )
    engine = FakeImageEngine()
    service = ImageTargetService(
        resolver=resolver,
        engine=engine,
        pull_timeout_seconds=600,
    )

    with pytest.raises(ImageTargetError, match="官方目标"):
        await service.pull_and_verify(
            registry_key="dockerhub",
            version=VERSION,
        )

    assert engine.pull_calls == []
