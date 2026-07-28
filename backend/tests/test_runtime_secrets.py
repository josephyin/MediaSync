import json
import stat
from pathlib import Path

import pytest

from app.core import runtime_secrets as runtime_secrets_module
from app.core.runtime_secrets import (
    RUNTIME_CONFIG_DIRECTORY,
    RUNTIME_SECRETS_FILENAME,
    RuntimeSecretsError,
    prepare_runtime_secrets,
)

SECRET_KEY = "existing-session-secret-key"
CREDENTIAL_KEY = "existing-credential-encryption-key"
ADMIN_PASSWORD = "strong-admin-password"


def runtime_secrets_path(data_directory: Path) -> Path:
    return data_directory / RUNTIME_CONFIG_DIRECTORY / RUNTIME_SECRETS_FILENAME


def test_first_start_generates_and_persists_runtime_secrets(tmp_path: Path) -> None:
    data_directory = tmp_path / "data"

    result = prepare_runtime_secrets(data_directory, environment={})
    secrets_path = runtime_secrets_path(data_directory)
    payload = json.loads(secrets_path.read_text(encoding="utf-8"))

    assert result.created is True
    assert result.admin_password_updated is False
    assert result.initial_admin_password == result.values.admin_password
    assert len(result.values.secret_key) >= 16
    assert len(result.values.credential_encryption_key) >= 16
    assert len(result.values.admin_password) >= 5
    assert payload == {
        "version": 1,
        "secret_key": result.values.secret_key,
        "credential_encryption_key": result.values.credential_encryption_key,
        "admin_password": result.values.admin_password,
    }
    assert stat.S_IMODE(secrets_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_restart_reuses_secrets_without_returning_password_again(tmp_path: Path) -> None:
    first = prepare_runtime_secrets(tmp_path, environment={})
    second = prepare_runtime_secrets(tmp_path, environment={})

    assert second.created is False
    assert second.admin_password_updated is False
    assert second.initial_admin_password is None
    assert second.values == first.values


def test_restart_tightens_persisted_secret_permissions(tmp_path: Path) -> None:
    first = prepare_runtime_secrets(tmp_path, environment={})
    secrets_path = runtime_secrets_path(tmp_path)
    secrets_path.parent.chmod(0o755)
    secrets_path.chmod(0o644)

    second = prepare_runtime_secrets(tmp_path, environment={})

    assert second.values == first.values
    assert stat.S_IMODE(secrets_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_first_start_persists_explicit_environment_values(tmp_path: Path) -> None:
    result = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
        },
    )

    assert result.values.as_environment() == {
        "SECRET_KEY": SECRET_KEY,
        "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
    }
    assert result.initial_admin_password is None


@pytest.mark.parametrize(
    ("environment_name", "replacement"),
    [
        ("SECRET_KEY", "different-session-secret-key"),
        ("CREDENTIAL_ENCRYPTION_KEY", "different-credential-key"),
    ],
)
def test_restart_rejects_implicit_key_rotation(
    tmp_path: Path,
    environment_name: str,
    replacement: str,
) -> None:
    original = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
        },
    )
    environment = original.values.as_environment()
    environment[environment_name] = replacement

    with pytest.raises(RuntimeSecretsError, match="Implicit key rotation is not supported"):
        prepare_runtime_secrets(tmp_path, environment=environment)

    reloaded = prepare_runtime_secrets(
        tmp_path,
        environment=original.values.as_environment(),
    )
    assert reloaded.values == original.values


def test_explicit_admin_password_resets_and_persists_offline(tmp_path: Path) -> None:
    first = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
        },
    )

    reset = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
            "ADMIN_PASSWORD": "new-strong-admin-password",
        },
    )
    reloaded = prepare_runtime_secrets(tmp_path, environment={})

    assert reset.created is False
    assert reset.admin_password_updated is True
    assert reset.initial_admin_password is None
    assert reset.values.secret_key == first.values.secret_key
    assert reset.values.credential_encryption_key == first.values.credential_encryption_key
    assert reset.values.admin_password == "new-strong-admin-password"
    assert reloaded.values == reset.values


def test_image_default_admin_password_does_not_reset_existing_data(
    tmp_path: Path,
) -> None:
    first = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
        },
    )

    restarted = prepare_runtime_secrets(
        tmp_path,
        environment={
            "ADMIN_PASSWORD": "admin",
            "IMAGE_DEFAULT_ADMIN_ONLY": "true",
        },
    )

    assert restarted.created is False
    assert restarted.admin_password_updated is False
    assert restarted.values == first.values


def test_custom_admin_password_overrides_image_default_for_existing_data(
    tmp_path: Path,
) -> None:
    prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
        },
    )

    restarted = prepare_runtime_secrets(
        tmp_path,
        environment={
            "ADMIN_PASSWORD": "new-strong-admin-password",
            "IMAGE_DEFAULT_ADMIN_ONLY": "true",
        },
    )

    assert restarted.admin_password_updated is True
    assert restarted.values.admin_password == "new-strong-admin-password"


@pytest.mark.parametrize("database_suffix", ["", "-wal", "-shm"])
def test_existing_database_files_require_original_cryptographic_keys(
    tmp_path: Path,
    database_suffix: str,
) -> None:
    data_directory = tmp_path / "data"
    data_directory.mkdir()
    (data_directory / f"mediasync.db{database_suffix}").touch()

    with pytest.raises(
        RuntimeSecretsError,
        match="Existing MediaSync database has no persisted runtime secrets",
    ):
        prepare_runtime_secrets(data_directory, environment={})

    assert not runtime_secrets_path(data_directory).exists()


def test_existing_database_can_import_original_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "mediasync.db"
    database_path.touch()

    result = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": SECRET_KEY,
            "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
        },
    )

    assert result.created is True
    assert result.values.secret_key == SECRET_KEY
    assert result.values.credential_encryption_key == CREDENTIAL_KEY
    assert result.initial_admin_password == result.values.admin_password


@pytest.mark.parametrize(
    "environment",
    [
        {"SECRET_KEY": SECRET_KEY},
        {"CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY},
    ],
)
def test_existing_database_rejects_partial_original_keys(
    tmp_path: Path,
    environment: dict[str, str],
) -> None:
    (tmp_path / "mediasync.db").touch()

    with pytest.raises(
        RuntimeSecretsError,
        match="Existing MediaSync database has no persisted runtime secrets",
    ):
        prepare_runtime_secrets(tmp_path, environment=environment)

    assert not runtime_secrets_path(tmp_path).exists()


def test_invalid_explicit_admin_password_does_not_create_secrets(tmp_path: Path) -> None:
    with pytest.raises(RuntimeSecretsError, match="ADMIN_PASSWORD must contain at least"):
        prepare_runtime_secrets(
            tmp_path,
            environment={
                "SECRET_KEY": SECRET_KEY,
                "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
                "ADMIN_PASSWORD": "1234",
            },
        )

    assert not runtime_secrets_path(tmp_path).exists()


def test_invalid_persisted_payload_is_not_replaced(tmp_path: Path) -> None:
    secrets_path = runtime_secrets_path(tmp_path)
    secrets_path.parent.mkdir(parents=True)
    secrets_path.write_text('{"version": 99}\n', encoding="utf-8")

    with pytest.raises(RuntimeSecretsError, match="Unsupported runtime secrets version"):
        prepare_runtime_secrets(tmp_path, environment={})

    assert json.loads(secrets_path.read_text(encoding="utf-8")) == {"version": 99}


def test_symbolic_link_secrets_file_is_rejected(tmp_path: Path) -> None:
    external_file = tmp_path / "external.json"
    external_file.write_text("{}\n", encoding="utf-8")
    secrets_path = runtime_secrets_path(tmp_path / "data")
    secrets_path.parent.mkdir(parents=True)
    secrets_path.symlink_to(external_file)

    with pytest.raises(RuntimeSecretsError, match="must not be a symbolic link"):
        prepare_runtime_secrets(tmp_path / "data", environment={})

    assert external_file.read_text(encoding="utf-8") == "{}\n"


def test_failed_atomic_replace_leaves_no_partial_secrets_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated filesystem failure")

    monkeypatch.setattr(runtime_secrets_module.os, "replace", fail_replace)

    with pytest.raises(RuntimeSecretsError, match="Unable to persist runtime secrets file"):
        prepare_runtime_secrets(tmp_path, environment={})

    secrets_path = runtime_secrets_path(tmp_path)
    assert not secrets_path.exists()
    assert not list(secrets_path.parent.glob(f".{secrets_path.name}.*.tmp"))
