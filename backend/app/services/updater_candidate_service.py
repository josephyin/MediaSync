from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.update_execution_gate import (
    CANDIDATE_TOKEN_PATTERN,
    PendingUpdateMarker,
    UpdateExecutionGate,
)
from app.services.update_snapshot_service import (
    HandoffDocument,
    fsync_directory,
    write_private_json,
)

INTERNAL_CANDIDATE_ENVIRONMENT = frozenset(
    {
        "MEDIASYNC_CANDIDATE_TOKEN",
        "MEDIASYNC_IMAGE_REVISION",
        "MEDIASYNC_IMAGE_DIGEST",
    }
)


class UpdaterCandidateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidatePreparation:
    marker: PendingUpdateMarker
    create_config: dict[str, Any]


class UpdaterCandidateService:
    def __init__(
        self,
        *,
        pending_path: Path,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._pending_path = Path(pending_path)
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def prepare(self, document: HandoffDocument) -> CandidatePreparation:
        marker = self._existing_marker(document)
        if marker is None:
            marker = PendingUpdateMarker(
                operation_id=document.operation_id,
                target_version=document.target_version,
                target_digest=document.target_digest,
                target_revision=document.target_revision,
                candidate_token=self._new_candidate_token(),
            )
            self._write_marker(marker)
        return CandidatePreparation(
            marker=marker,
            create_config=build_candidate_create_config(document, marker),
        )

    def _existing_marker(
        self,
        document: HandoffDocument,
    ) -> PendingUpdateMarker | None:
        marker = UpdateExecutionGate(
            pending_path=str(self._pending_path)
        ).read_pending_marker()
        if marker is None:
            return None
        if isinstance(marker, str):
            raise UpdaterCandidateError(marker)
        if (
            marker.operation_id != document.operation_id
            or marker.target_version != document.target_version
            or marker.target_digest != document.target_digest
            or marker.target_revision != document.target_revision
        ):
            raise UpdaterCandidateError("已有候选验证标记与 handoff 不匹配")
        return marker

    def _new_candidate_token(self) -> str:
        token = self._token_factory()
        if not isinstance(token, str) or not CANDIDATE_TOKEN_PATTERN.fullmatch(token):
            raise UpdaterCandidateError("候选验证令牌生成失败")
        return token

    def _write_marker(self, marker: PendingUpdateMarker) -> None:
        directory = self._pending_path.parent
        temporary = directory / f".{self._pending_path.name}.{secrets.token_hex(8)}.tmp"
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise UpdaterCandidateError("候选验证标记目录不安全")
            os.chmod(directory, 0o700)
            if self._pending_path.is_symlink():
                raise UpdaterCandidateError("候选验证标记路径不安全")
            write_private_json(
                temporary,
                {
                    "operation_id": marker.operation_id,
                    "target_version": marker.target_version,
                    "target_digest": marker.target_digest,
                    "target_revision": marker.target_revision,
                    "candidate_token": marker.candidate_token,
                    "mode": marker.mode,
                },
            )
            try:
                os.link(temporary, self._pending_path)
            except FileExistsError as exc:
                raise UpdaterCandidateError("候选验证标记已存在") from exc
            temporary.unlink()
            fsync_directory(directory)
        except UpdaterCandidateError:
            raise
        except OSError as exc:
            raise UpdaterCandidateError("无法持久化候选验证标记") from exc
        finally:
            temporary.unlink(missing_ok=True)


def build_candidate_create_config(
    document: HandoffDocument,
    marker: PendingUpdateMarker,
) -> dict[str, Any]:
    if (
        marker.operation_id != document.operation_id
        or marker.target_version != document.target_version
        or marker.target_digest != document.target_digest
        or marker.target_revision != document.target_revision
        or not CANDIDATE_TOKEN_PATTERN.fullmatch(marker.candidate_token)
    ):
        raise UpdaterCandidateError("候选创建配置与 handoff 不匹配")
    candidate = document.candidate
    environment = [
        item
        for item in candidate.env
        if item.partition("=")[0] not in INTERNAL_CANDIDATE_ENVIRONMENT
    ]
    environment.extend(
        (
            f"MEDIASYNC_CANDIDATE_TOKEN={marker.candidate_token}",
            f"MEDIASYNC_IMAGE_REVISION={marker.target_revision}",
            f"MEDIASYNC_IMAGE_DIGEST={marker.target_digest}",
        )
    )
    endpoint_config = {
        name: {"Aliases": list(aliases)}
        for name, aliases in candidate.networks.items()
    }
    return {
        "Image": document.target_image,
        "Env": environment,
        "User": candidate.user,
        "Labels": dict(candidate.labels),
        "ExposedPorts": {port: {} for port in candidate.exposed_ports},
        "HostConfig": {
            "Mounts": [
                {
                    "Type": mount.type,
                    "Source": mount.source,
                    "Target": mount.target,
                    "ReadOnly": mount.read_only,
                }
                for mount in candidate.mounts
            ],
            "PortBindings": candidate.port_bindings,
            "RestartPolicy": candidate.restart_policy,
            "NetworkMode": candidate.network_mode,
            "Dns": list(candidate.dns),
            "GroupAdd": list(candidate.group_add),
            "ReadonlyRootfs": candidate.readonly_rootfs,
        },
        "NetworkingConfig": {"EndpointsConfig": endpoint_config},
    }
