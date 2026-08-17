from __future__ import annotations

import json
import os
import posixpath
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from app.services.docker_capability_service import (
    COMPOSE_PROJECT_LABEL,
    CONTAINER_ID_PATTERN,
    OFFICIAL_SOURCE,
    DockerEngineError,
    validate_current_container,
)
from app.services.image_target_service import (
    DIGEST_PATTERN,
    VerifiedImageTarget,
    validate_resolved_target,
)

CONTAINER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
UPDATER_COMMAND = ["python", "-m", "app.updater"]
UPDATE_ROLE_LABEL = "io.mediasync.update.role"
UPDATE_OPERATION_LABEL = "io.mediasync.update.operation"
UPDATER_ROLE = "updater"
CANDIDATE_ROLE = "candidate"
ALLOWED_RESTART_POLICIES = {"", "no", "always", "unless-stopped", "on-failure"}
ALLOWED_MOUNT_TYPES = {"bind", "volume"}
GENERATED_LABEL_PREFIXES = ("com.docker.", "org.opencontainers.image.")


class UpdaterHandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class SafeMount:
    type: str
    source: str
    target: str
    read_only: bool

    def docker_mount(self) -> dict[str, Any]:
        return {
            "Type": self.type,
            "Source": self.source,
            "Target": self.target,
            "ReadOnly": self.read_only,
        }


@dataclass(frozen=True)
class SafeDevice:
    path_on_host: str
    path_in_container: str
    cgroup_permissions: str

    def docker_device(self) -> dict[str, str]:
        return {
            "PathOnHost": self.path_on_host,
            "PathInContainer": self.path_in_container,
            "CgroupPermissions": self.cgroup_permissions,
        }


@dataclass(frozen=True)
class CandidateContainerTemplate:
    container_id: str
    name: str
    env: tuple[str, ...]
    user: str
    labels: dict[str, str]
    mounts: tuple[SafeMount, ...]
    exposed_ports: tuple[str, ...]
    port_bindings: dict[str, list[dict[str, str]]]
    restart_policy: dict[str, Any]
    network_mode: str
    networks: dict[str, tuple[str, ...]]
    dns: tuple[str, ...]
    group_add: tuple[str, ...]
    readonly_rootfs: bool
    devices: tuple[SafeDevice, ...] = ()

    def data_mount(self) -> SafeMount:
        return next(mount for mount in self.mounts if mount.target == "/data")

    def docker_socket_mount(self, socket_path: str) -> SafeMount:
        return next(mount for mount in self.mounts if mount.target == socket_path)

    def to_candidate_create_config(self, *, image: str) -> dict[str, Any]:
        endpoint_config = {
            name: {"Aliases": list(aliases)} for name, aliases in self.networks.items()
        }
        return {
            "Image": image,
            "Env": list(self.env),
            "User": self.user,
            "Labels": dict(self.labels),
            "ExposedPorts": {port: {} for port in self.exposed_ports},
            "HostConfig": {
                "Mounts": [mount.docker_mount() for mount in self.mounts],
                "PortBindings": self.port_bindings,
                "RestartPolicy": self.restart_policy,
                "NetworkMode": self.network_mode,
                "Dns": list(self.dns),
                "GroupAdd": list(self.group_add),
                "ReadonlyRootfs": self.readonly_rootfs,
                "Devices": [device.docker_device() for device in self.devices],
            },
            "NetworkingConfig": {"EndpointsConfig": endpoint_config},
        }


@dataclass(frozen=True)
class UpdaterHandoffIntent:
    schema_version: int
    operation_id: str
    current_container_id: str
    source_image_id: str
    source_image_reference: str
    source_version: str
    source_digest: str | None
    target_version: str
    target_digest: str
    target_revision: str
    target_image: str
    candidate: CandidateContainerTemplate

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["candidate"]["mounts"] = [
            asdict(mount) for mount in self.candidate.mounts
        ]
        return payload


class ContainerCreator(Protocol):
    async def create_container(
        self,
        *,
        name: str,
        config: dict[str, Any],
    ) -> str: ...


class UpdaterHandoffStore:
    def __init__(self, *, directory: str) -> None:
        self.directory = Path(directory)

    def write(self, intent: UpdaterHandoffIntent) -> Path:
        validate_operation_id(intent.operation_id)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        final_path = self.directory / f"{intent.operation_id}.handoff.json"
        if final_path.exists():
            raise UpdaterHandoffError("updater handoff 已存在")
        temporary_path = self.directory / f".{intent.operation_id}.{secrets.token_hex(8)}.tmp"
        encoded = json.dumps(
            intent.to_json(), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, final_path)
            directory_descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return final_path


class UpdaterHandoffService:
    def __init__(
        self,
        *,
        engine: ContainerCreator,
        store: UpdaterHandoffStore,
        socket_path: str,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.engine = engine
        self.store = store
        self.socket_path = socket_path
        self.nonce_factory = nonce_factory or (lambda: secrets.token_hex(6))

    async def prepare(
        self,
        *,
        operation_id: str,
        current_container: dict[str, Any],
        target: VerifiedImageTarget,
    ) -> tuple[str, Path]:
        validate_operation_id(operation_id)
        validate_resolved_target(
            target,
            registry_key=target.registry,
            version=target.version,
        )
        template = extract_candidate_template(
            current_container,
            socket_path=self.socket_path,
        )
        source = extract_source_image(current_container)
        intent = UpdaterHandoffIntent(
            schema_version=2,
            operation_id=operation_id,
            current_container_id=template.container_id,
            source_image_id=source[0],
            source_image_reference=source[1],
            source_version=source[2],
            source_digest=source[3],
            target_version=target.version,
            target_digest=target.digest,
            target_revision=target.revision,
            target_image=target.immutable_reference,
            candidate=template,
        )
        name = f"mediasync-updater-{self.nonce_factory()}"
        if not CONTAINER_NAME_PATTERN.fullmatch(name):
            raise UpdaterHandoffError("updater 容器名称无效")
        config = build_updater_create_config(
            operation_id=operation_id,
            target=target,
            template=template,
            socket_path=self.socket_path,
        )
        path = self.store.write(intent)
        try:
            container_id = await self.engine.create_container(name=name, config=config)
        except DockerEngineError as exc:
            path.unlink(missing_ok=True)
            raise UpdaterHandoffError("无法准备 updater 助手容器") from exc
        return container_id, path


def extract_candidate_template(
    container: dict[str, Any],
    *,
    socket_path: str,
) -> CandidateContainerTemplate:
    rejection = validate_current_container(container)
    if rejection is not None:
        raise UpdaterHandoffError(rejection[1])
    container_id = container.get("Id")
    name_value = container.get("Name")
    name = name_value.removeprefix("/") if isinstance(name_value, str) else ""
    if not isinstance(container_id, str) or not CONTAINER_ID_PATTERN.fullmatch(container_id):
        raise UpdaterHandoffError("当前容器标识无效")
    if not CONTAINER_NAME_PATTERN.fullmatch(name):
        raise UpdaterHandoffError("当前容器名称无效")
    config = require_mapping(container.get("Config"), "当前容器配置不完整")
    host = require_mapping(container.get("HostConfig"), "当前容器宿主配置不完整")
    if host.get("Privileged") is True or nonempty_list(host.get("CapAdd")):
        raise UpdaterHandoffError("高权限容器不支持一键更新")
    device_requests = host.get("DeviceRequests")
    if device_requests is not None and device_requests != []:
        raise UpdaterHandoffError("带 GPU 设备请求的容器暂不支持一键更新")
    network_mode = string_value(host.get("NetworkMode"), "default")
    if network_mode in {"host", "none"} or network_mode.startswith("container:"):
        raise UpdaterHandoffError("当前网络模式不支持一键更新")
    mounts = extract_mounts(container.get("Mounts"))
    require_mount(mounts, "/data", writable=True)
    require_mount(mounts, socket_path, mount_type="bind")
    labels = extract_labels(config.get("Labels"))
    if COMPOSE_PROJECT_LABEL in labels:
        raise UpdaterHandoffError("Compose 管理容器不支持一键更新")
    return CandidateContainerTemplate(
        container_id=container_id,
        name=name,
        env=string_tuple(config.get("Env")),
        user=string_value(config.get("User")),
        labels={
            key: value
            for key, value in labels.items()
            if not key.startswith(GENERATED_LABEL_PREFIXES)
        },
        mounts=mounts,
        exposed_ports=extract_exposed_ports(config.get("ExposedPorts")),
        port_bindings=extract_port_bindings(host.get("PortBindings")),
        restart_policy=extract_restart_policy(host.get("RestartPolicy")),
        network_mode=network_mode,
        networks=extract_networks(container),
        dns=string_tuple(host.get("Dns")),
        group_add=string_tuple(host.get("GroupAdd")),
        readonly_rootfs=host.get("ReadonlyRootfs") is True,
        devices=extract_devices(host.get("Devices")),
    )


def extract_source_image(
    container: dict[str, Any],
) -> tuple[str, str, str, str | None]:
    image_id = container.get("Image")
    config = require_mapping(container.get("Config"), "当前容器配置不完整")
    reference = config.get("Image")
    labels = extract_labels(config.get("Labels"))
    version = labels.get("org.opencontainers.image.version")
    if (
        not isinstance(image_id, str)
        or not DIGEST_PATTERN.fullmatch(image_id)
        or not isinstance(reference, str)
        or not reference
        or not isinstance(version, str)
        or not version
    ):
        raise UpdaterHandoffError("当前镜像元数据不完整")
    _, separator, digest = reference.rpartition("@")
    source_digest = digest if separator and DIGEST_PATTERN.fullmatch(digest) else None
    return image_id, reference, version, source_digest


def build_updater_create_config(
    *,
    operation_id: str,
    target: VerifiedImageTarget,
    template: CandidateContainerTemplate,
    socket_path: str,
) -> dict[str, Any]:
    data = template.data_mount()
    docker_socket = template.docker_socket_mount(socket_path)
    return {
        "Image": target.immutable_reference,
        "Cmd": UPDATER_COMMAND,
        "Env": [f"MEDIASYNC_UPDATE_OPERATION_ID={operation_id}"],
        "Labels": {
            "io.mediasync.updater": "true",
            UPDATE_ROLE_LABEL: UPDATER_ROLE,
            UPDATE_OPERATION_LABEL: operation_id,
            "org.opencontainers.image.source": OFFICIAL_SOURCE,
        },
        "HostConfig": {
            "AutoRemove": False,
            "RestartPolicy": {
                "Name": "unless-stopped",
                "MaximumRetryCount": 0,
            },
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Mounts": [
                SafeMount(data.type, data.source, "/data", False).docker_mount(),
                SafeMount(
                    "bind", docker_socket.source, socket_path, False
                ).docker_mount(),
            ],
        },
    }


def validate_operation_id(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UpdaterHandoffError("更新操作标识无效") from exc
    if str(parsed) != value:
        raise UpdaterHandoffError("更新操作标识无效")


def extract_mounts(value: object) -> tuple[SafeMount, ...]:
    if not isinstance(value, list):
        raise UpdaterHandoffError("当前容器挂载信息不完整")
    mounts: list[SafeMount] = []
    targets: set[str] = set()
    for item in value:
        mapping = require_mapping(item, "当前容器挂载信息无效")
        mount_type = mapping.get("Type")
        source = mapping.get("Name") if mount_type == "volume" else mapping.get("Source")
        target = mapping.get("Destination")
        if mount_type not in ALLOWED_MOUNT_TYPES:
            raise UpdaterHandoffError("存在不支持的容器挂载类型")
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            raise UpdaterHandoffError("当前容器挂载信息无效")
        if target in targets:
            raise UpdaterHandoffError("当前容器存在重复挂载目标")
        targets.add(target)
        mounts.append(SafeMount(mount_type, source, target, mapping.get("RW") is not True))
    return tuple(mounts)


def require_mount(
    mounts: tuple[SafeMount, ...],
    target: str,
    *,
    writable: bool = False,
    mount_type: str | None = None,
) -> None:
    matches = [mount for mount in mounts if mount.target == target]
    if len(matches) != 1:
        raise UpdaterHandoffError(f"当前容器必须唯一挂载 {target}")
    mount = matches[0]
    if writable and mount.read_only:
        raise UpdaterHandoffError(f"当前容器的 {target} 必须可写")
    if mount_type is not None and mount.type != mount_type:
        raise UpdaterHandoffError(f"当前容器的 {target} 挂载类型无效")


def extract_labels(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise UpdaterHandoffError("当前容器标签无效")
    return value


def extract_devices(value: object) -> tuple[SafeDevice, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise UpdaterHandoffError("当前容器设备映射无效")
    devices: list[SafeDevice] = []
    targets: set[str] = set()
    for item in value:
        mapping = require_mapping(item, "当前容器设备映射无效")
        path_on_host = mapping.get("PathOnHost")
        path_in_container = mapping.get("PathInContainer")
        permissions = mapping.get("CgroupPermissions")
        if not _valid_device_path(path_on_host) or not _valid_device_path(
            path_in_container
        ):
            raise UpdaterHandoffError("当前容器设备映射无效")
        if (
            not isinstance(permissions, str)
            or not permissions
            or len(set(permissions)) != len(permissions)
            or not set(permissions).issubset({"r", "w", "m"})
        ):
            raise UpdaterHandoffError("当前容器设备映射权限无效")
        if path_in_container in targets:
            raise UpdaterHandoffError("当前容器存在重复设备映射目标")
        targets.add(path_in_container)
        devices.append(
            SafeDevice(
                path_on_host=path_on_host,
                path_in_container=path_in_container,
                cgroup_permissions=permissions,
            )
        )
    return tuple(devices)


def _valid_device_path(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 < len(value) <= 4096
        and value.startswith("/")
        and "\x00" not in value
        and posixpath.normpath(value) == value
    )


def extract_port_bindings(value: object) -> dict[str, list[dict[str, str]]]:
    if value is None:
        return {}
    mapping = require_mapping(value, "当前容器端口绑定无效")
    result: dict[str, list[dict[str, str]]] = {}
    for port, bindings in mapping.items():
        if not isinstance(port, str) or not isinstance(bindings, list):
            raise UpdaterHandoffError("当前容器端口绑定无效")
        clean: list[dict[str, str]] = []
        for binding in bindings:
            item = require_mapping(binding, "当前容器端口绑定无效")
            host_ip = string_value(item.get("HostIp"))
            host_port = string_value(item.get("HostPort"))
            if host_port and not host_port.isdigit():
                raise UpdaterHandoffError("当前容器端口绑定无效")
            clean.append({"HostIp": host_ip, "HostPort": host_port})
        result[port] = clean
    return result


def extract_exposed_ports(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    mapping = require_mapping(value, "当前容器暴露端口无效")
    if not all(isinstance(port, str) and isinstance(item, dict) for port, item in mapping.items()):
        raise UpdaterHandoffError("当前容器暴露端口无效")
    return tuple(mapping)


def extract_restart_policy(value: object) -> dict[str, Any]:
    mapping = require_mapping(value or {}, "当前容器重启策略无效")
    name = string_value(mapping.get("Name"), "no")
    retries = mapping.get("MaximumRetryCount", 0)
    if name not in ALLOWED_RESTART_POLICIES or not isinstance(retries, int) or retries < 0:
        raise UpdaterHandoffError("当前容器重启策略无效")
    return {"Name": name, "MaximumRetryCount": retries}


def extract_networks(container: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    settings = require_mapping(container.get("NetworkSettings"), "当前容器网络信息不完整")
    networks = require_mapping(settings.get("Networks"), "当前容器网络信息不完整")
    result: dict[str, tuple[str, ...]] = {}
    for name, endpoint in networks.items():
        if not isinstance(name, str):
            raise UpdaterHandoffError("当前容器网络信息无效")
        endpoint_mapping = require_mapping(endpoint, "当前容器网络信息无效")
        result[name] = string_tuple(endpoint_mapping.get("Aliases"))
    return result


def require_mapping(value: object, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpdaterHandoffError(message)
    return value


def string_value(value: object, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise UpdaterHandoffError("当前容器配置字段类型无效")
    return value


def string_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise UpdaterHandoffError("当前容器配置列表无效")
    return tuple(value)


def nonempty_list(value: object) -> bool:
    return isinstance(value, list) and bool(value)
