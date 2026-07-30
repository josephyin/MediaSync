from __future__ import annotations

import logging
import secrets
import threading
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings
from app.core.runtime_secrets import RuntimeSecretsError, update_runtime_admin_password

MINIMUM_ONLINE_PASSWORD_LENGTH = 8
MAXIMUM_ONLINE_PASSWORD_LENGTH = 128
BOOTSTRAP_ADMIN_PASSWORD = "admin"
logger = logging.getLogger(__name__)


class AdminCredentialError(RuntimeError):
    """管理员凭证无法按契约更新。"""


class InvalidCurrentPasswordError(AdminCredentialError):
    """当前密码校验失败。"""


class AdminPasswordPersistenceError(AdminCredentialError):
    """管理员密码无法安全持久化。"""


class AdminCredentialStore:
    """管理员密码和会话修订号的进程内唯一入口。"""

    def __init__(
        self,
        *,
        password: str,
        revision: int = 0,
        runtime_secrets_path: Path | None = None,
    ) -> None:
        if revision < 0:
            raise ValueError("revision must be nonnegative")
        self._password = password
        self._revision = revision
        self._runtime_secrets_path = (
            Path(runtime_secrets_path) if runtime_secrets_path is not None else None
        )
        self._lock = threading.Lock()

    @property
    def password_change_supported(self) -> bool:
        return self._runtime_secrets_path is not None

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def verify(self, password: str) -> bool:
        with self._lock:
            return secrets.compare_digest(password, self._password)

    def change_password(
        self,
        *,
        current_password: str,
        new_password: str,
    ) -> int:
        with self._lock:
            if (
                len(new_password) < MINIMUM_ONLINE_PASSWORD_LENGTH
                or len(new_password) > MAXIMUM_ONLINE_PASSWORD_LENGTH
                or new_password.strip() == ""
                or secrets.compare_digest(new_password, BOOTSTRAP_ADMIN_PASSWORD)
            ):
                logger.warning("admin_password_change_failed reason=validation")
                raise InvalidCurrentPasswordError("当前密码或新密码不符合要求")
            if not secrets.compare_digest(current_password, self._password):
                logger.warning("admin_password_change_failed reason=validation")
                raise InvalidCurrentPasswordError("当前密码或新密码不符合要求")
            if secrets.compare_digest(new_password, self._password):
                logger.warning("admin_password_change_failed reason=validation")
                raise InvalidCurrentPasswordError("当前密码或新密码不符合要求")
            if self._runtime_secrets_path is None:
                raise AdminCredentialError("当前部署模式不支持在线修改密码")

            try:
                updated = update_runtime_admin_password(
                    self._runtime_secrets_path,
                    current_password=self._password,
                    new_password=new_password,
                    expected_revision=self._revision,
                )
            except RuntimeSecretsError as exc:
                logger.error("admin_password_change_failed reason=persistence")
                raise AdminPasswordPersistenceError(
                    "管理员密码无法安全持久化"
                ) from exc

            self._password = updated.admin_password
            self._revision = updated.admin_session_revision
            logger.info("admin_password_change_succeeded source=web")
            return self._revision


@lru_cache
def get_admin_credential_store() -> AdminCredentialStore:
    settings = get_settings()
    runtime_path = (
        Path(settings.runtime_secrets_path)
        if settings.admin_password_change_supported and settings.runtime_secrets_path
        else None
    )
    return AdminCredentialStore(
        password=settings.admin_password,
        revision=settings.admin_session_revision,
        runtime_secrets_path=runtime_path,
    )
