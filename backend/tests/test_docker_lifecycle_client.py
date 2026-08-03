from __future__ import annotations

import json

import httpx
import pytest

from app.services.docker_capability_service import (
    DockerEngineClient,
    DockerEngineError,
)

CONTAINER_ID = "a" * 64
NETWORK = "mediasync_default"


def client(
    handler,
) -> DockerEngineClient:
    return DockerEngineClient(
        socket_path="/unused.sock",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_start_stop_wait_use_bounded_explicit_docker_contracts() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json={"StatusCode": 0, "Error": None})
        return httpx.Response(204)

    engine = client(handler)

    await engine.start_container(CONTAINER_ID)
    await engine.stop_container(CONTAINER_ID, timeout_seconds=90)
    assert await engine.wait_container(CONTAINER_ID) == 0

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", f"/containers/{CONTAINER_ID}/start"),
        ("POST", f"/containers/{CONTAINER_ID}/stop"),
        ("POST", f"/containers/{CONTAINER_ID}/wait"),
    ]
    assert requests[1].url.params["t"] == "90"
    assert requests[2].url.params["condition"] == "not-running"


@pytest.mark.asyncio
async def test_already_started_or_stopped_is_idempotent() -> None:
    engine = client(lambda _: httpx.Response(304))

    await engine.start_container(CONTAINER_ID)
    await engine.stop_container(CONTAINER_ID, timeout_seconds=30)


@pytest.mark.asyncio
async def test_restart_policy_update_uses_restricted_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Warnings": None})

    await client(handler).update_restart_policy(
        CONTAINER_ID,
        restart_policy={"Name": "no", "MaximumRetryCount": 0},
    )

    assert requests[0].method == "POST"
    assert requests[0].url.path == f"/containers/{CONTAINER_ID}/update"
    assert json.loads(requests[0].content) == {
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}
    }


@pytest.mark.asyncio
async def test_rename_and_remove_keep_exact_identity_without_deleting_volumes() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    engine = client(handler)

    await engine.rename_container(CONTAINER_ID, name="mediasync-previous-abc123")
    await engine.remove_container(CONTAINER_ID)

    assert requests[0].url.params["name"] == "mediasync-previous-abc123"
    assert requests[1].method == "DELETE"
    assert requests[1].url.params["v"] == "0"
    assert "force" not in requests[1].url.params


@pytest.mark.asyncio
async def test_remove_missing_container_is_idempotent() -> None:
    await client(lambda _: httpx.Response(404)).remove_container(CONTAINER_ID)


@pytest.mark.asyncio
async def test_network_operations_use_restricted_payload() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    engine = client(handler)
    await engine.connect_network(
        NETWORK,
        container_id=CONTAINER_ID,
        aliases=("mediasync", "media-sync"),
    )
    await engine.disconnect_network(NETWORK, container_id=CONTAINER_ID)

    assert requests[0].url.path == f"/networks/{NETWORK}/connect"
    assert json.loads(requests[0].content) == {
        "Container": CONTAINER_ID,
        "EndpointConfig": {"Aliases": ["mediasync", "media-sync"]},
    }
    assert requests[1].url.path == f"/networks/{NETWORK}/disconnect"
    assert json.loads(requests[1].content) == {
        "Container": CONTAINER_ID,
        "Force": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (lambda engine: engine.start_container("../escape"), "容器标识格式无效"),
        (
            lambda engine: engine.stop_container(CONTAINER_ID, timeout_seconds=0),
            "容器停止超时时间无效",
        ),
        (
            lambda engine: engine.rename_container(CONTAINER_ID, name="bad/name"),
            "容器名称格式无效",
        ),
        (
            lambda engine: engine.update_restart_policy(
                CONTAINER_ID,
                restart_policy={"Name": "invalid", "MaximumRetryCount": 0},
            ),
            "容器重启策略格式无效",
        ),
        (
            lambda engine: engine.connect_network(
                "../docker.sock",
                container_id=CONTAINER_ID,
            ),
            "网络标识格式无效",
        ),
    ],
)
async def test_invalid_identifiers_are_rejected_before_engine_request(
    operation,
    message: str,
) -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("不应调用 Docker Engine")

    with pytest.raises(DockerEngineError, match=message):
        await operation(client(unexpected_request))


@pytest.mark.asyncio
async def test_engine_error_body_is_not_exposed() -> None:
    engine = client(
        lambda _: httpx.Response(
            409,
            json={"message": "secret host path /volume1/private is busy"},
        )
    )

    with pytest.raises(DockerEngineError) as error:
        await engine.rename_container(CONTAINER_ID, name="mediasync-next")

    assert str(error.value) == "无法重命名容器"
    assert "/volume1/private" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"StatusCode": True},
        {"StatusCode": -1},
        {"StatusCode": 0, "Error": {"Message": "failed"}},
    ],
)
async def test_wait_rejects_invalid_or_error_result(payload: dict) -> None:
    engine = client(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(DockerEngineError):
        await engine.wait_container(CONTAINER_ID)
