from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from app.services.docker_capability_service import (
    OFFICIAL_SOURCE,
    DockerCapabilityService,
    DockerEngineError,
    validate_current_container,
)

CONTAINER_ID = "a" * 64
OTHER_CONTAINER_ID = "b" * 64


def official_container(container_id: str = CONTAINER_ID) -> dict[str, Any]:
    return {
        "Id": container_id,
        "Config": {
            "Cmd": ["python", "-m", "app.appliance"],
            "Labels": {
                "org.opencontainers.image.source": OFFICIAL_SOURCE,
                "org.opencontainers.image.title": "MediaSync",
            },
        },
        "Mounts": [
            {
                "Type": "volume",
                "Source": "mediasync-data",
                "Destination": "/data",
                "RW": True,
            }
        ],
    }


def official_summary(container_id: str = CONTAINER_ID) -> dict[str, Any]:
    container = official_container(container_id)
    return {
        "Id": container_id,
        "State": "running",
        "Command": "python -m app.appliance",
        "Labels": container["Config"]["Labels"],
        "Mounts": container["Mounts"],
    }


class FakeDockerEngine:
    def __init__(
        self,
        *,
        containers: dict[str, dict[str, Any]] | None = None,
        summaries: list[dict[str, Any]] | None = None,
        ping_error: bool = False,
    ) -> None:
        self.containers = containers or {}
        self.summaries = summaries or []
        self.ping_error = ping_error
        self.inspect_calls: list[str] = []
        self.list_calls = 0

    async def ping(self) -> None:
        if self.ping_error:
            raise DockerEngineError("不可访问")

    async def inspect_container(self, container_id: str) -> dict[str, Any]:
        self.inspect_calls.append(container_id)
        try:
            return self.containers[container_id]
        except KeyError as exc:
            raise DockerEngineError("容器不存在") from exc

    async def list_containers(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        return self.summaries


def service(
    engine: FakeDockerEngine,
    *,
    socket_state: str = "socket",
    explicit_id: str = "",
    hostname: str = "",
    monotonic_clock=lambda: 1.0,
) -> DockerCapabilityService:
    return DockerCapabilityService(
        socket_path="/var/run/docker.sock",
        explicit_container_id=explicit_id,
        hostname=hostname,
        engine=engine,
        cache_seconds=30,
        socket_probe=lambda _: socket_state,
        monotonic_clock=monotonic_clock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("socket_state", "reason_code"),
    [
        ("missing", "socket_missing"),
        ("invalid_type", "socket_invalid"),
        ("inaccessible", "socket_inaccessible"),
    ],
)
async def test_socket_failures_safely_disable_capability(
    socket_state: str,
    reason_code: str,
) -> None:
    result = await service(FakeDockerEngine(), socket_state=socket_state).probe()

    assert result.reason_code == reason_code
    assert result.socket_available is False
    assert result.engine_available is False


@pytest.mark.asyncio
async def test_engine_failure_does_not_expose_exception_details() -> None:
    result = await service(FakeDockerEngine(ping_error=True)).probe()

    assert result.reason_code == "engine_unavailable"
    assert result.socket_available is True
    assert result.engine_available is False
    assert result.message == "Docker Engine API 不可访问或权限不足"


@pytest.mark.asyncio
async def test_explicit_container_id_has_highest_priority() -> None:
    engine = FakeDockerEngine(
        containers={CONTAINER_ID: official_container()},
        summaries=[official_summary(OTHER_CONTAINER_ID)],
    )

    result = await service(
        engine,
        explicit_id=CONTAINER_ID.upper(),
        hostname=OTHER_CONTAINER_ID,
    ).probe()

    assert result.reason_code == "ready"
    assert engine.inspect_calls == [CONTAINER_ID]
    assert engine.list_calls == 0


@pytest.mark.asyncio
async def test_hostname_identifies_default_docker_container() -> None:
    hostname_id = CONTAINER_ID[:12]
    engine = FakeDockerEngine(containers={hostname_id: official_container()})

    result = await service(engine, hostname=hostname_id).probe()

    assert result.reason_code == "ready"
    assert engine.inspect_calls == [hostname_id]
    assert engine.list_calls == 0


@pytest.mark.asyncio
async def test_unique_official_fallback_candidate_is_inspected() -> None:
    engine = FakeDockerEngine(
        containers={CONTAINER_ID: official_container()},
        summaries=[official_summary()],
    )

    result = await service(engine, hostname="custom-hostname").probe()

    assert result.reason_code == "ready"
    assert engine.inspect_calls == [CONTAINER_ID]
    assert engine.list_calls == 1


@pytest.mark.asyncio
async def test_resolver_returns_full_id_for_custom_hostname() -> None:
    engine = FakeDockerEngine(
        containers={CONTAINER_ID: official_container()},
        summaries=[official_summary()],
    )

    container = await service(
        engine,
        hostname="mediasync-nas",
    ).resolve_current_container()

    assert container["Id"] == CONTAINER_ID
    assert engine.inspect_calls == [CONTAINER_ID]
    assert engine.list_calls == 1


@pytest.mark.asyncio
async def test_resolver_ignores_stopped_previous_appliance() -> None:
    stopped = official_summary(OTHER_CONTAINER_ID)
    stopped["State"] = "exited"
    engine = FakeDockerEngine(
        containers={CONTAINER_ID: official_container()},
        summaries=[official_summary(), stopped],
    )

    container = await service(
        engine,
        hostname="mediasync-nas",
    ).resolve_current_container()

    assert container["Id"] == CONTAINER_ID
    assert engine.inspect_calls == [CONTAINER_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "summaries",
    [
        [],
        [official_summary(), official_summary(OTHER_CONTAINER_ID)],
    ],
)
async def test_zero_or_multiple_candidates_are_rejected(
    summaries: list[dict[str, Any]],
) -> None:
    engine = FakeDockerEngine(summaries=summaries)

    result = await service(engine).probe()

    assert result.reason_code == "container_not_identified"
    assert result.container_identified is False


@pytest.mark.parametrize(
    ("mutate", "reason_code"),
    [
        (
            lambda item: item["Config"]["Labels"].update(
                {"org.opencontainers.image.source": "https://example.com/unofficial"}
            ),
            "container_unofficial",
        ),
        (
            lambda item: item["Config"]["Labels"].update(
                {"com.docker.compose.project": "mediasync"}
            ),
            "compose_managed",
        ),
        (
            lambda item: item["Config"].update({"Cmd": ["python", "-m", "app.main"]}),
            "not_appliance",
        ),
        (lambda item: item.update({"Mounts": []}), "data_mount_missing"),
    ],
)
def test_container_security_contract_rejects_invalid_runtime(
    mutate,
    reason_code: str,
) -> None:
    container = deepcopy(official_container())
    mutate(container)

    rejection = validate_current_container(container)

    assert rejection is not None
    assert rejection[0] == reason_code


@pytest.mark.asyncio
async def test_probe_result_is_cached() -> None:
    moments = iter([1.0, 2.0])
    engine = FakeDockerEngine(
        containers={CONTAINER_ID: official_container()},
    )
    checker = service(
        engine,
        explicit_id=CONTAINER_ID,
        monotonic_clock=lambda: next(moments),
    )

    first = await checker.probe()
    second = await checker.probe()

    assert first.reason_code == second.reason_code == "ready"
    assert engine.inspect_calls == [CONTAINER_ID]
