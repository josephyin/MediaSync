from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from app.services.update_snapshot_service import (
    UpdaterResultJournal,
    UpdateSnapshotError,
    UpdateSnapshotService,
    read_handoff,
)
from app.services.updater_handoff_service import (
    CandidateContainerTemplate,
    SafeMount,
    UpdaterHandoffIntent,
    UpdaterHandoffStore,
)

OPERATION_ID = "12345678-1234-4234-9234-123456789abc"
CONTAINER_ID = "a" * 64
SOURCE_IMAGE_ID = f"sha256:{'b' * 64}"
TARGET_DIGEST = f"sha256:{'c' * 64}"
SECRET_TEXT = "never-include-this-secret-in-manifest"


def candidate() -> CandidateContainerTemplate:
    return CandidateContainerTemplate(
        container_id=CONTAINER_ID,
        name="MediaSync",
        env=(f"ADMIN_PASSWORD={SECRET_TEXT}",),
        user="",
        labels={},
        mounts=(
            SafeMount("volume", "mediasync-data", "/data", False),
            SafeMount("bind", "/var/run/docker.sock", "/var/run/docker.sock", False),
        ),
        exposed_ports=("9090/tcp",),
        port_bindings={"9090/tcp": [{"HostIp": "", "HostPort": "9090"}]},
        restart_policy={"Name": "unless-stopped", "MaximumRetryCount": 0},
        network_mode="bridge",
        networks={"bridge": ("MediaSync",)},
        dns=(),
        group_add=(),
        readonly_rootfs=False,
    )


def write_handoff(data_directory: Path) -> Path:
    store = UpdaterHandoffStore(
        directory=str(data_directory / "update" / "operations")
    )
    return store.write(
        UpdaterHandoffIntent(
            schema_version=1,
            operation_id=OPERATION_ID,
            current_container_id=CONTAINER_ID,
            source_image_id=SOURCE_IMAGE_ID,
            source_image_reference="josephyjq/mediasync:v0.2.0-rc.9",
            source_version="v0.2.0-rc.9",
            source_digest=None,
            target_version="v0.3.0-rc.1",
            target_digest=TARGET_DIGEST,
            target_image=f"josephyjq/mediasync@{TARGET_DIGEST}",
            candidate=candidate(),
        )
    )


def create_data_files(data_directory: Path) -> Path:
    database = data_directory / "mediasync.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
    connection.execute("INSERT INTO alembic_version VALUES ('0007_update_operations')")
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('before-update')")
    connection.commit()
    connection.close()
    secrets_path = data_directory / "config" / "runtime-secrets.json"
    secrets_path.parent.mkdir(parents=True)
    secrets_path.write_text(
        json.dumps({"secret_key": SECRET_TEXT}),
        encoding="utf-8",
    )
    return write_handoff(data_directory)


def read_sample(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT value FROM sample").fetchone()
    finally:
        connection.close()
    assert row is not None
    return row[0]


def test_snapshot_manifest_is_complete_private_and_contains_no_secret(
    tmp_path: Path,
) -> None:
    handoff = create_data_files(tmp_path)
    service = UpdateSnapshotService(data_directory=str(tmp_path))

    snapshot = service.create(operation_id=OPERATION_ID, handoff_path=handoff)
    directory, manifest = service.verify(operation_id=OPERATION_ID)

    assert directory == snapshot
    assert manifest.alembic_revision == "0007_update_operations"
    assert manifest.source_image_id == SOURCE_IMAGE_ID
    assert {item.source_path for item in manifest.files} == {
        "mediasync.db",
        "config/runtime-secrets.json",
        "update/handoff.json",
    }
    manifest_text = (snapshot / "manifest.json").read_text(encoding="utf-8")
    assert SECRET_TEXT not in manifest_text
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in snapshot.iterdir()
        if path.is_file()
    )


def test_snapshot_includes_live_wal_and_shm_as_one_restore_unit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mediasync.db"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
    connection.execute("INSERT INTO alembic_version VALUES ('0007_update_operations')")
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('stored-in-wal')")
    connection.commit()
    secrets_path = tmp_path / "config" / "runtime-secrets.json"
    secrets_path.parent.mkdir(parents=True)
    secrets_path.write_text('{"secret_key":"safe"}', encoding="utf-8")
    handoff = write_handoff(tmp_path)
    assert (tmp_path / "mediasync.db-wal").exists()
    assert (tmp_path / "mediasync.db-shm").exists()

    try:
        service = UpdateSnapshotService(data_directory=str(tmp_path))
        service.create(operation_id=OPERATION_ID, handoff_path=handoff)
    finally:
        connection.close()

    _, manifest = service.verify(operation_id=OPERATION_ID)
    assert {item.source_path for item in manifest.files}.issuperset(
        {"mediasync.db", "mediasync.db-wal", "mediasync.db-shm"}
    )


def test_restore_round_trip_replaces_database_and_secrets_and_removes_sidecars(
    tmp_path: Path,
) -> None:
    handoff = create_data_files(tmp_path)
    service = UpdateSnapshotService(data_directory=str(tmp_path))
    service.create(operation_id=OPERATION_ID, handoff_path=handoff)

    database = tmp_path / "mediasync.db"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE sample SET value='candidate-version'")
    connection.commit()
    connection.close()
    (tmp_path / "config" / "runtime-secrets.json").write_text(
        '{"secret_key":"candidate-secret"}', encoding="utf-8"
    )
    (tmp_path / "mediasync.db-wal").write_bytes(b"candidate-wal")
    (tmp_path / "mediasync.db-shm").write_bytes(b"candidate-shm")

    service.restore(operation_id=OPERATION_ID)

    assert read_sample(database) == "before-update"
    assert SECRET_TEXT in (
        tmp_path / "config" / "runtime-secrets.json"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / "mediasync.db-wal").exists()
    assert not (tmp_path / "mediasync.db-shm").exists()


def test_corrupted_snapshot_is_rejected_before_restore(tmp_path: Path) -> None:
    handoff = create_data_files(tmp_path)
    service = UpdateSnapshotService(data_directory=str(tmp_path))
    snapshot = service.create(operation_id=OPERATION_ID, handoff_path=handoff)
    (snapshot / "database.sqlite").write_bytes(b"corrupted")
    original_database = (tmp_path / "mediasync.db").read_bytes()

    with pytest.raises(UpdateSnapshotError, match="校验失败"):
        service.restore(operation_id=OPERATION_ID)

    assert (tmp_path / "mediasync.db").read_bytes() == original_database


def test_snapshot_failure_does_not_leave_complete_or_temporary_snapshot(
    tmp_path: Path,
) -> None:
    handoff = create_data_files(tmp_path)
    (tmp_path / "config" / "runtime-secrets.json").unlink()
    service = UpdateSnapshotService(data_directory=str(tmp_path))

    with pytest.raises(UpdateSnapshotError, match="快照源文件"):
        service.create(operation_id=OPERATION_ID, handoff_path=handoff)

    backup_root = tmp_path / "backups" / "updates"
    assert not (backup_root / OPERATION_ID).exists()
    assert list(backup_root.iterdir()) == []


def test_handoff_extra_field_or_wrong_official_target_is_rejected(
    tmp_path: Path,
) -> None:
    handoff = create_data_files(tmp_path)
    payload = json.loads(handoff.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    handoff.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UpdateSnapshotError, match="内容无效"):
        read_handoff(handoff, expected_operation_id=OPERATION_ID)

    payload.pop("unexpected")
    payload["target_image"] = f"evil.example/mediasync@{TARGET_DIGEST}"
    handoff.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UpdateSnapshotError, match="官方仓库"):
        read_handoff(handoff, expected_operation_id=OPERATION_ID)


def test_result_journal_enforces_state_order_and_terminal_immutability(
    tmp_path: Path,
) -> None:
    journal = UpdaterResultJournal(directory=str(tmp_path / "operations"))
    first = journal.start(operation_id=OPERATION_ID)
    assert first.status == "snapshotting"
    assert first.sequence == 1

    with pytest.raises(UpdateSnapshotError, match="状态转换无效"):
        journal.transition(operation_id=OPERATION_ID, status="success")

    second = journal.transition(operation_id=OPERATION_ID, status="switching")
    journal.transition(operation_id=OPERATION_ID, status="verifying")
    terminal = journal.transition(operation_id=OPERATION_ID, status="success")

    assert second.sequence == 2
    assert terminal.sequence == 4
    assert stat.S_IMODE((tmp_path / "operations").stat().st_mode) == 0o700
    assert stat.S_IMODE(
        (tmp_path / "operations" / f"{OPERATION_ID}.json").stat().st_mode
    ) == 0o600
    with pytest.raises(UpdateSnapshotError, match="终态结果不可修改"):
        journal.transition(operation_id=OPERATION_ID, status="rolling_back")
