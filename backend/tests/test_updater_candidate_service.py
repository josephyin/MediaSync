from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from app.services.update_snapshot_service import read_handoff
from app.services.updater_candidate_service import (
    UpdaterCandidateError,
    UpdaterCandidateService,
)
from app.services.updater_handoff_service import (
    CandidateContainerTemplate,
    SafeDevice,
    SafeMount,
    UpdaterHandoffIntent,
    UpdaterHandoffStore,
)

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
CONTAINER_ID = "a" * 64
SOURCE_IMAGE_ID = f"sha256:{'b' * 64}"
TARGET_DIGEST = f"sha256:{'c' * 64}"
TARGET_REVISION = "d" * 40
CANDIDATE_TOKEN = "candidate-token-0123456789-abcdef"
SECRET_TEXT = "do-not-copy-to-pending"


def write_handoff(tmp_path: Path):
    candidate = CandidateContainerTemplate(
        container_id=CONTAINER_ID,
        name="MediaSync",
        env=(
            f"ADMIN_PASSWORD={SECRET_TEXT}",
            "TZ=Asia/Shanghai",
            "MEDIASYNC_CANDIDATE_TOKEN=forged-token",
            f"MEDIASYNC_IMAGE_REVISION={'e' * 40}",
            f"MEDIASYNC_IMAGE_DIGEST=sha256:{'f' * 64}",
        ),
        user="1000:1000",
        labels={"user.label": "keep"},
        mounts=(SafeMount("volume", "mediasync-data", "/data", False),),
        exposed_ports=("9090/tcp",),
        port_bindings={"9090/tcp": [{"HostIp": "", "HostPort": "9090"}]},
        restart_policy={"Name": "unless-stopped", "MaximumRetryCount": 0},
        network_mode="bridge",
        networks={"bridge": ("MediaSync",)},
        dns=("1.1.1.1",),
        group_add=("100",),
        readonly_rootfs=False,
        devices=(SafeDevice("/dev/dri", "/dev/dri", "rwm"),),
    )
    path = UpdaterHandoffStore(
        directory=str(tmp_path / "update" / "operations")
    ).write(
        UpdaterHandoffIntent(
            schema_version=2,
            operation_id=OPERATION_ID,
            current_container_id=CONTAINER_ID,
            source_image_id=SOURCE_IMAGE_ID,
            source_image_reference="josephyjq/mediasync:v0.2.0-rc.9",
            source_version="v0.2.0-rc.9",
            source_digest=None,
            target_version="v0.3.0-rc.1",
            target_digest=TARGET_DIGEST,
            target_revision=TARGET_REVISION,
            target_image=f"josephyjq/mediasync@{TARGET_DIGEST}",
            candidate=candidate,
        )
    )
    return read_handoff(path, expected_operation_id=OPERATION_ID)


def test_prepare_writes_private_minimal_marker_and_trusted_candidate_identity(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    pending = tmp_path / "update" / "pending.json"
    service = UpdaterCandidateService(
        pending_path=pending,
        token_factory=lambda: CANDIDATE_TOKEN,
    )

    preparation = service.prepare(document)

    assert preparation.create_config["HostConfig"]["Devices"] == [
        {
            "PathOnHost": "/dev/dri",
            "PathInContainer": "/dev/dri",
            "CgroupPermissions": "rwm",
        }
    ]

    payload = json.loads(pending.read_text(encoding="utf-8"))
    assert payload == {
        "operation_id": OPERATION_ID,
        "target_version": "v0.3.0-rc.1",
        "target_digest": TARGET_DIGEST,
        "target_revision": TARGET_REVISION,
        "candidate_token": CANDIDATE_TOKEN,
        "mode": "candidate_validation",
    }
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600
    assert stat.S_IMODE(pending.parent.stat().st_mode) == 0o700
    assert SECRET_TEXT not in pending.read_text(encoding="utf-8")

    environment = preparation.create_config["Env"]
    assert f"ADMIN_PASSWORD={SECRET_TEXT}" in environment
    assert environment.count(f"MEDIASYNC_CANDIDATE_TOKEN={CANDIDATE_TOKEN}") == 1
    assert environment.count(f"MEDIASYNC_IMAGE_REVISION={TARGET_REVISION}") == 1
    assert environment.count(f"MEDIASYNC_IMAGE_DIGEST={TARGET_DIGEST}") == 1
    assert not any("forged-token" in item for item in environment)
    assert preparation.create_config["Image"] == f"josephyjq/mediasync@{TARGET_DIGEST}"
    assert preparation.create_config["Labels"]["io.mediasync.update.role"] == "candidate"
    assert preparation.create_config["Labels"]["io.mediasync.update.operation"] == OPERATION_ID


def test_retry_reuses_existing_token_instead_of_rotating_candidate_identity(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    calls = 0

    def token_factory() -> str:
        nonlocal calls
        calls += 1
        return CANDIDATE_TOKEN

    service = UpdaterCandidateService(
        pending_path=tmp_path / "update" / "pending.json",
        token_factory=token_factory,
    )

    first = service.prepare(document)
    second = service.prepare(document)

    assert calls == 1
    assert first.marker == second.marker
    assert first.create_config == second.create_config


def test_conflicting_existing_marker_is_never_overwritten(tmp_path: Path) -> None:
    document = write_handoff(tmp_path)
    pending = tmp_path / "update" / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "operation_id": "22345678-1234-4234-9234-123456789abc",
                "target_version": "v0.3.0-rc.1",
                "target_digest": TARGET_DIGEST,
                "target_revision": TARGET_REVISION,
                "candidate_token": CANDIDATE_TOKEN,
                "mode": "candidate_validation",
            }
        ),
        encoding="utf-8",
    )
    original = pending.read_bytes()

    with pytest.raises(UpdaterCandidateError, match="不匹配"):
        UpdaterCandidateService(pending_path=pending).prepare(document)

    assert pending.read_bytes() == original


def test_invalid_generated_token_does_not_create_pending_marker(tmp_path: Path) -> None:
    document = write_handoff(tmp_path)
    pending = tmp_path / "update" / "pending.json"

    with pytest.raises(UpdaterCandidateError, match="令牌生成失败"):
        UpdaterCandidateService(
            pending_path=pending,
            token_factory=lambda: "too-short",
        ).prepare(document)

    assert not pending.exists()


def test_symlink_pending_path_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    document = write_handoff(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text("keep", encoding="utf-8")
    pending = tmp_path / "update" / "pending.json"
    pending.symlink_to(target)

    with pytest.raises(UpdaterCandidateError, match="路径不安全"):
        UpdaterCandidateService(
            pending_path=pending,
            token_factory=lambda: CANDIDATE_TOKEN,
        ).prepare(document)

    assert target.read_text(encoding="utf-8") == "keep"
