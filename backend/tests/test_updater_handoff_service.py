from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.services.docker_capability_service import (
    APPLIANCE_COMMAND,
    OFFICIAL_SOURCE,
    DockerEngineClient,
    DockerEngineError,
)
from app.services.image_target_service import VerifiedImageTarget
from app.services.updater_handoff_service import (
    UPDATER_COMMAND,
    UpdaterHandoffError,
    UpdaterHandoffService,
    UpdaterHandoffStore,
    extract_candidate_template,
)

CONTAINER_ID = "a" * 64
UPDATER_ID = "b" * 64
SOURCE_IMAGE_ID = f"sha256:{'e' * 64}"
OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
DIGEST = f"sha256:{'c' * 64}"
SOCKET_PATH = "/var/run/docker.sock"


def target() -> VerifiedImageTarget:
    return VerifiedImageTarget(
        registry="dockerhub",
        repository="josephyjq/mediasync",
        version="v0.3.0-rc.1",
        digest=DIGEST,
        revision="d" * 40,
    )


def current_container() -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "Name": "/MediaSync",
        "Image": SOURCE_IMAGE_ID,
        "Config": {
            "Image": "josephyjq/mediasync:v0.2.0-rc.9",
            "Cmd": APPLIANCE_COMMAND,
            "Env": ["ADMIN_PASSWORD=secret", "TZ=Asia/Shanghai"],
            "User": "1000:1000",
            "ExposedPorts": {"9090/tcp": {}},
            "Labels": {
                "org.opencontainers.image.source": OFFICIAL_SOURCE,
                "org.opencontainers.image.title": "MediaSync",
                "org.opencontainers.image.revision": "old-revision",
                "org.opencontainers.image.version": "v0.2.0-rc.9",
                "user.label": "keep",
            },
        },
        "HostConfig": {
            "Privileged": False,
            "CapAdd": None,
            "Devices": None,
            "PortBindings": {
                "9090/tcp": [{"HostIp": "", "HostPort": "9090"}]
            },
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "bridge",
            "Dns": ["1.1.1.1"],
            "GroupAdd": ["100"],
            "ReadonlyRootfs": False,
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": "mediasync-data",
                "Source": "/var/lib/docker/volumes/mediasync-data/_data",
                "Destination": "/data",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": SOCKET_PATH,
                "Destination": SOCKET_PATH,
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/volume1/media",
                "Destination": "/media",
                "RW": False,
            },
        ],
        "NetworkSettings": {
            "Networks": {
                "bridge": {
                    "Aliases": ["MediaSync"],
                    "IPAddress": "172.17.0.2",
                    "MacAddress": "00:00:00:00:00:00",
                }
            }
        },
    }


class FakeCreator:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_container(self, *, name: str, config: dict[str, Any]) -> str:
        self.calls.append((name, config))
        if self.fail:
            raise DockerEngineError("create failed")
        return UPDATER_ID


def test_candidate_template_keeps_only_rebuild_whitelist() -> None:
    template = extract_candidate_template(
        current_container(),
        socket_path=SOCKET_PATH,
    )

    assert template.name == "MediaSync"
    assert template.env == ("ADMIN_PASSWORD=secret", "TZ=Asia/Shanghai")
    assert template.labels == {"user.label": "keep"}
    assert [mount.target for mount in template.mounts] == [
        "/data",
        SOCKET_PATH,
        "/media",
    ]
    candidate = template.to_candidate_create_config(image=f"image@{DIGEST}")
    assert candidate["ExposedPorts"] == {"9090/tcp": {}}
    assert candidate["HostConfig"]["PortBindings"]["9090/tcp"][0][
        "HostPort"
    ] == "9090"
    assert candidate["NetworkingConfig"] == {
        "EndpointsConfig": {"bridge": {"Aliases": ["MediaSync"]}}
    }
    serialized = json.dumps(candidate)
    assert "IPAddress" not in serialized
    assert "MacAddress" not in serialized
    assert "Privileged" not in serialized
    assert "CapAdd" not in serialized


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item["HostConfig"].update(Privileged=True), "高权限"),
        (lambda item: item["HostConfig"].update(CapAdd=["SYS_ADMIN"]), "高权限"),
        (lambda item: item["HostConfig"].update(Devices=[{}]), "设备映射"),
        (lambda item: item["HostConfig"].update(NetworkMode="host"), "网络模式"),
        (lambda item: item["Mounts"][0].update(RW=False), "/data 必须可写"),
    ],
)
def test_unsupported_or_elevated_current_configuration_is_rejected(
    mutate,
    message: str,
) -> None:
    container = deepcopy(current_container())
    mutate(container)

    with pytest.raises(UpdaterHandoffError, match=message):
        extract_candidate_template(container, socket_path=SOCKET_PATH)


@pytest.mark.asyncio
async def test_prepare_writes_private_intent_and_creates_restricted_updater(
    tmp_path: Path,
) -> None:
    creator = FakeCreator()
    service = UpdaterHandoffService(
        engine=creator,
        store=UpdaterHandoffStore(directory=str(tmp_path / "operations")),
        socket_path=SOCKET_PATH,
        nonce_factory=lambda: "fixednonce",
    )

    container_id, path = await service.prepare(
        operation_id=OPERATION_ID,
        current_container=current_container(),
        target=target(),
    )

    assert container_id == UPDATER_ID
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    intent = json.loads(path.read_text(encoding="utf-8"))
    assert intent["target_image"] == f"josephyjq/mediasync@{DIGEST}"
    assert intent["source_image_id"] == SOURCE_IMAGE_ID
    assert intent["source_image_reference"] == "josephyjq/mediasync:v0.2.0-rc.9"
    assert intent["source_version"] == "v0.2.0-rc.9"
    assert intent["source_digest"] is None
    assert intent["candidate"]["env"][0] == "ADMIN_PASSWORD=secret"
    name, config = creator.calls[0]
    assert name == "mediasync-updater-fixednonce"
    assert config["Image"] == f"josephyjq/mediasync@{DIGEST}"
    assert config["Cmd"] == UPDATER_COMMAND
    assert "ExposedPorts" not in config
    assert config["HostConfig"]["NetworkMode"] == "none"
    assert config["HostConfig"]["AutoRemove"] is True
    assert config["HostConfig"]["CapDrop"] == ["ALL"]
    mounts = config["HostConfig"]["Mounts"]
    assert {(item["Source"], item["Target"]) for item in mounts} == {
        ("mediasync-data", "/data"),
        (SOCKET_PATH, SOCKET_PATH),
    }


@pytest.mark.asyncio
async def test_create_failure_removes_unconsumed_handoff(tmp_path: Path) -> None:
    service = UpdaterHandoffService(
        engine=FakeCreator(fail=True),
        store=UpdaterHandoffStore(directory=str(tmp_path)),
        socket_path=SOCKET_PATH,
        nonce_factory=lambda: "fixednonce",
    )

    with pytest.raises(UpdaterHandoffError, match="无法准备"):
        await service.prepare(
            operation_id=OPERATION_ID,
            current_container=current_container(),
            target=target(),
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_docker_client_create_contract_uses_only_name_and_json_body() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"Id": UPDATER_ID, "Warnings": []})

    client = DockerEngineClient(
        socket_path="/unused.sock",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    container_id = await client.create_container(
        name="mediasync-updater-test",
        config={"Image": f"josephyjq/mediasync@{DIGEST}"},
    )

    assert container_id == UPDATER_ID
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/containers/create"
    assert requests[0].url.params["name"] == "mediasync-updater-test"
    assert json.loads(requests[0].content) == {
        "Image": f"josephyjq/mediasync@{DIGEST}"
    }
