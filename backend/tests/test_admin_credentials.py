from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.core import runtime_secrets as runtime_secrets_module
from app.core.admin_credentials import (
    AdminCredentialStore,
    AdminPasswordPersistenceError,
    InvalidCurrentPasswordError,
)
from app.core.runtime_secrets import (
    RUNTIME_CONFIG_DIRECTORY,
    RUNTIME_SECRETS_FILENAME,
    prepare_runtime_secrets,
)

OLD_PASSWORD = "old-strong-password"
NEW_PASSWORD = "new-strong-password"


def build_store(tmp_path: Path) -> tuple[AdminCredentialStore, Path]:
    prepared = prepare_runtime_secrets(
        tmp_path,
        environment={
            "SECRET_KEY": "test-session-secret-key",
            "CREDENTIAL_ENCRYPTION_KEY": "test-credential-secret-key",
            "ADMIN_PASSWORD": OLD_PASSWORD,
        },
    )
    path = tmp_path / RUNTIME_CONFIG_DIRECTORY / RUNTIME_SECRETS_FILENAME
    return (
        AdminCredentialStore(
            password=prepared.values.admin_password,
            revision=prepared.values.admin_session_revision,
            runtime_secrets_path=path,
        ),
        path,
    )


def test_password_change_persists_before_switching_memory(tmp_path: Path) -> None:
    store, path = build_store(tmp_path)

    revision = store.change_password(
        current_password=OLD_PASSWORD,
        new_password=NEW_PASSWORD,
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert revision == 1
    assert payload["admin_password"] == NEW_PASSWORD
    assert payload["admin_session_revision"] == 1
    assert not store.verify(OLD_PASSWORD)
    assert store.verify(NEW_PASSWORD)


@pytest.mark.parametrize(
    "new_password",
    ["admin", "short", "        ", OLD_PASSWORD],
)
def test_invalid_password_change_has_no_state_change(
    tmp_path: Path,
    new_password: str,
) -> None:
    store, path = build_store(tmp_path)
    before = path.read_bytes()

    with pytest.raises(InvalidCurrentPasswordError):
        store.change_password(
            current_password=OLD_PASSWORD,
            new_password=new_password,
        )

    assert path.read_bytes() == before
    assert store.revision == 0
    assert store.verify(OLD_PASSWORD)


def test_persistence_failure_keeps_old_in_memory_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, path = build_store(tmp_path)
    before = path.read_bytes()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(runtime_secrets_module.os, "replace", fail_replace)

    with pytest.raises(AdminPasswordPersistenceError):
        store.change_password(
            current_password=OLD_PASSWORD,
            new_password=NEW_PASSWORD,
        )

    assert path.read_bytes() == before
    assert store.revision == 0
    assert store.verify(OLD_PASSWORD)


def test_concurrent_changes_allow_at_most_one_success(tmp_path: Path) -> None:
    store, _path = build_store(tmp_path)

    def attempt(new_password: str) -> bool:
        try:
            store.change_password(
                current_password=OLD_PASSWORD,
                new_password=new_password,
            )
        except InvalidCurrentPasswordError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                attempt,
                ("first-new-password", "second-new-password"),
            )
        )

    assert results.count(True) == 1
    assert store.revision == 1
