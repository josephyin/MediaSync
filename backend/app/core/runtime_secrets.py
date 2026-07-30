from __future__ import annotations

import json
import os
import secrets
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

RUNTIME_SECRETS_VERSION = 1
RUNTIME_CONFIG_DIRECTORY = "config"
RUNTIME_SECRETS_FILENAME = "runtime-secrets.json"
DEFAULT_DATABASE_FILENAME = "mediasync.db"

_MINIMUM_KEY_LENGTH = 16
_MINIMUM_ADMIN_PASSWORD_LENGTH = 5
_IMAGE_DEFAULT_ADMIN_PASSWORD = "admin"
_IMAGE_DEFAULT_ADMIN_PASSWORD_FLAG = "IMAGE_DEFAULT_ADMIN_ONLY"
_TRUE_VALUES = {"1", "true", "yes", "on"}
_SECRET_ENVIRONMENT_KEYS = {
    "secret_key": "SECRET_KEY",
    "credential_encryption_key": "CREDENTIAL_ENCRYPTION_KEY",
}


class RuntimeSecretsError(RuntimeError):
    """Raised when the persisted Appliance credentials cannot be used safely."""


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    secret_key: str
    credential_encryption_key: str
    admin_password: str
    admin_session_revision: int = 0

    def as_environment(self) -> dict[str, str]:
        return {
            "SECRET_KEY": self.secret_key,
            "CREDENTIAL_ENCRYPTION_KEY": self.credential_encryption_key,
            "ADMIN_PASSWORD": self.admin_password,
            "ADMIN_SESSION_REVISION": str(self.admin_session_revision),
        }


@dataclass(frozen=True, slots=True)
class RuntimeSecretsPreparation:
    values: RuntimeSecrets
    created: bool
    admin_password_updated: bool
    initial_admin_password: str | None


def prepare_runtime_secrets(
    data_directory: Path,
    *,
    environment: Mapping[str, str] | None = None,
    database_filename: str = DEFAULT_DATABASE_FILENAME,
) -> RuntimeSecretsPreparation:
    """Load or create the persisted secrets used by Appliance child processes.

    Cryptographic keys become immutable after the first successful write. An
    explicit administrator password may be changed offline and is persisted
    atomically for subsequent container starts.
    """

    if not database_filename or Path(database_filename).name != database_filename:
        raise ValueError("database_filename must be a plain filename")

    runtime_environment = os.environ if environment is None else environment
    data_path = Path(data_directory)
    config_path = data_path / RUNTIME_CONFIG_DIRECTORY
    secrets_path = config_path / RUNTIME_SECRETS_FILENAME

    _ensure_config_directory(data_path, config_path)

    if secrets_path.is_symlink():
        raise RuntimeSecretsError(
            f"Runtime secrets file must not be a symbolic link: {secrets_path}"
        )

    if secrets_path.exists():
        current = _read_runtime_secrets(secrets_path)
        _validate_immutable_environment(current, runtime_environment)

        requested_password = _requested_admin_password_for_existing_data(
            runtime_environment
        )
        if requested_password is None or secrets.compare_digest(
            requested_password,
            current.admin_password,
        ):
            return RuntimeSecretsPreparation(
                values=current,
                created=False,
                admin_password_updated=False,
                initial_admin_password=None,
            )

        _validate_admin_password(requested_password)
        updated = replace(
            current,
            admin_password=requested_password,
            admin_session_revision=current.admin_session_revision + 1,
        )
        _write_runtime_secrets(secrets_path, updated)
        return RuntimeSecretsPreparation(
            values=updated,
            created=False,
            admin_password_updated=True,
            initial_admin_password=None,
        )

    secret_key = _environment_value(runtime_environment, "SECRET_KEY")
    credential_key = _environment_value(runtime_environment, "CREDENTIAL_ENCRYPTION_KEY")
    database_path = data_path / database_filename

    if _database_files_exist(database_path) and (secret_key is None or credential_key is None):
        missing = [
            name
            for name, value in (
                ("SECRET_KEY", secret_key),
                ("CREDENTIAL_ENCRYPTION_KEY", credential_key),
            )
            if value is None
        ]
        raise RuntimeSecretsError(
            "Existing MediaSync database has no persisted runtime secrets. "
            f"Provide the original {', '.join(missing)} before starting."
        )

    secret_key = secret_key or secrets.token_urlsafe(48)
    credential_key = credential_key or secrets.token_urlsafe(48)
    _validate_key("SECRET_KEY", secret_key)
    _validate_key("CREDENTIAL_ENCRYPTION_KEY", credential_key)

    requested_password = _environment_value(runtime_environment, "ADMIN_PASSWORD")
    generated_password = requested_password is None
    admin_password = requested_password or secrets.token_urlsafe(18)
    _validate_admin_password(admin_password)

    created = RuntimeSecrets(
        secret_key=secret_key,
        credential_encryption_key=credential_key,
        admin_password=admin_password,
    )
    _write_runtime_secrets(secrets_path, created)
    return RuntimeSecretsPreparation(
        values=created,
        created=True,
        admin_password_updated=False,
        initial_admin_password=admin_password if generated_password else None,
    )


def _ensure_config_directory(data_path: Path, config_path: Path) -> None:
    if data_path.is_symlink():
        raise RuntimeSecretsError(f"Data directory must not be a symbolic link: {data_path}")
    if data_path.exists() and not data_path.is_dir():
        raise RuntimeSecretsError(f"Data path is not a directory: {data_path}")

    try:
        data_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeSecretsError(f"Unable to create data directory: {data_path}") from exc

    if config_path.is_symlink():
        raise RuntimeSecretsError(
            f"Runtime configuration directory must not be a symbolic link: {config_path}"
        )
    if config_path.exists() and not config_path.is_dir():
        raise RuntimeSecretsError(
            f"Runtime configuration path is not a directory: {config_path}"
        )

    try:
        config_path.mkdir(mode=0o700, exist_ok=True)
        config_path.chmod(0o700)
    except OSError as exc:
        raise RuntimeSecretsError(
            f"Unable to prepare runtime configuration directory: {config_path}"
        ) from exc


def _read_runtime_secrets(path: Path) -> RuntimeSecrets:
    if not path.is_file():
        raise RuntimeSecretsError(f"Runtime secrets path is not a regular file: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeSecretsError(f"Unable to read runtime secrets file: {path}") from exc

    if not isinstance(payload, dict):
        raise RuntimeSecretsError("Runtime secrets payload must be a JSON object")
    if payload.get("version") != RUNTIME_SECRETS_VERSION:
        raise RuntimeSecretsError(
            f"Unsupported runtime secrets version: {payload.get('version')!r}"
        )

    values = RuntimeSecrets(
        secret_key=_required_string(payload, "secret_key"),
        credential_encryption_key=_required_string(payload, "credential_encryption_key"),
        admin_password=_required_string(payload, "admin_password"),
        admin_session_revision=_optional_nonnegative_integer(
            payload,
            "admin_session_revision",
            default=0,
        ),
    )
    _validate_key("SECRET_KEY", values.secret_key)
    _validate_key("CREDENTIAL_ENCRYPTION_KEY", values.credential_encryption_key)
    _validate_admin_password(values.admin_password)

    try:
        path.chmod(0o600)
    except OSError as exc:
        raise RuntimeSecretsError(f"Unable to secure runtime secrets file: {path}") from exc
    return values


def _write_runtime_secrets(path: Path, values: RuntimeSecrets) -> None:
    payload = {
        "version": RUNTIME_SECRETS_VERSION,
        "secret_key": values.secret_key,
        "credential_encryption_key": values.credential_encryption_key,
        "admin_password": values.admin_password,
        "admin_session_revision": values.admin_session_revision,
    }
    encoded = f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
    temporary_path: Path | None = None

    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        path.chmod(0o600)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise RuntimeSecretsError(f"Unable to persist runtime secrets file: {path}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    directory_descriptor = os.open(path, flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _validate_immutable_environment(
    current: RuntimeSecrets,
    environment: Mapping[str, str],
) -> None:
    for field_name, environment_name in _SECRET_ENVIRONMENT_KEYS.items():
        requested = _environment_value(environment, environment_name)
        if requested is None:
            continue
        _validate_key(environment_name, requested)
        persisted = getattr(current, field_name)
        if not secrets.compare_digest(requested, persisted):
            raise RuntimeSecretsError(
                f"{environment_name} does not match the persisted runtime secret. "
                "Implicit key rotation is not supported."
            )


def _database_files_exist(database_path: Path) -> bool:
    return any(
        candidate.exists()
        for candidate in (
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        )
    )


def _environment_value(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name)
    return value if value else None


def _requested_admin_password_for_existing_data(
    environment: Mapping[str, str],
) -> str | None:
    requested_password = _environment_value(environment, "ADMIN_PASSWORD")
    default_only = (
        environment.get(_IMAGE_DEFAULT_ADMIN_PASSWORD_FLAG, "").strip().lower()
        in _TRUE_VALUES
    )
    if default_only and requested_password == _IMAGE_DEFAULT_ADMIN_PASSWORD:
        return None
    return requested_password


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeSecretsError(f"Runtime secrets field is missing or invalid: {name}")
    return value


def _optional_nonnegative_integer(
    payload: dict[str, Any],
    name: str,
    *,
    default: int,
) -> int:
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeSecretsError(f"Runtime secrets field is missing or invalid: {name}")
    return value


def update_runtime_admin_password(
    path: Path,
    *,
    current_password: str,
    new_password: str,
    expected_revision: int,
) -> RuntimeSecrets:
    """Atomically update the persisted Appliance password and session revision."""

    secrets_path = Path(path)
    if secrets_path.is_symlink():
        raise RuntimeSecretsError(
            f"Runtime secrets file must not be a symbolic link: {secrets_path}"
        )
    current = _read_runtime_secrets(secrets_path)
    if current.admin_session_revision != expected_revision or not secrets.compare_digest(
        current.admin_password,
        current_password,
    ):
        raise RuntimeSecretsError("Administrator credential changed concurrently")

    _validate_admin_password(new_password)
    updated = replace(
        current,
        admin_password=new_password,
        admin_session_revision=current.admin_session_revision + 1,
    )
    _write_runtime_secrets(secrets_path, updated)
    return updated


def _validate_key(name: str, value: str) -> None:
    if len(value) < _MINIMUM_KEY_LENGTH:
        raise RuntimeSecretsError(
            f"{name} must contain at least {_MINIMUM_KEY_LENGTH} characters"
        )


def _validate_admin_password(value: str) -> None:
    if len(value) < _MINIMUM_ADMIN_PASSWORD_LENGTH:
        raise RuntimeSecretsError(
            "ADMIN_PASSWORD must contain at least "
            f"{_MINIMUM_ADMIN_PASSWORD_LENGTH} characters"
        )
