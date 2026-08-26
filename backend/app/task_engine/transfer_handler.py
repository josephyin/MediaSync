from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRequestError,
    ProviderWriteUncertainError,
)
from app.models import CloudAccount, CloudFile, Task
from app.models.base import utcnow
from app.providers import get_provider
from app.providers.base import CloudDriveProvider
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.services.subscription_service import decrypt_share_password
from app.services.transfer_operation import (
    TransferCancelledError,
    TransferOperationResult,
    TransferSpec,
    execute_transfer,
)
from app.task_engine.handlers import TaskExecutionContext, TaskInvocation, TaskOutcome

SessionFactory = Callable[[], Session]
ProviderFactory = Callable[[str, str, str | None], CloudDriveProvider]
TokenLoader = Callable[[CloudAccount], str]
TokenPersister = Callable[[CloudAccount, CloudDriveProvider], bool]
FailureStatus = Literal["retry", "failed", "waiting_credential"]

_CREDENTIAL_ERROR_MARKERS = (
    "refresh token",
    "refreshtoken",
    "access token",
    "accesstoken",
    "invalid_grant",
    "token expired",
    "token is expired",
    "token invalid",
    "token is invalid",
    "unauthorized",
)


class TransferPayloadError(ValueError):
    pass


class TransferSourceNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class TransferPayloadV1:
    subscription_id: int
    file_id: int

    @classmethod
    def parse(cls, invocation: TaskInvocation) -> TransferPayloadV1:
        if invocation.task_type != "transfer":
            raise TransferPayloadError("transfer handler requires task type 'transfer'")
        if invocation.payload_version != 1:
            raise TransferPayloadError(
                f"unsupported transfer payload version {invocation.payload_version}"
            )
        if invocation.subscription_id is None or invocation.file_id is None:
            raise TransferPayloadError(
                "transfer payload v1 requires subscription_id and file_id"
            )
        if invocation.payload:
            raise TransferPayloadError("transfer payload v1 does not accept extra fields")
        return cls(
            subscription_id=invocation.subscription_id,
            file_id=invocation.file_id,
        )


@dataclass(frozen=True)
class _TransferSource:
    file_id: int
    account_id: int
    provider_type: str
    refresh_token: str
    target_drive_id: str | None
    spec: TransferSpec


@dataclass(frozen=True)
class _FailureDisposition:
    status: FailureStatus
    error_code: str
    safe_message: str
    blocked_reason: str | None = None


class TransferTaskHandler:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        provider_factory: ProviderFactory = get_provider,
        token_loader: TokenLoader = get_decrypted_token,
        token_persister: TokenPersister = persist_provider_token,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._provider_factory = provider_factory
        self._token_loader = token_loader
        self._token_persister = token_persister
        self._clock = clock

    async def __call__(self, context: TaskExecutionContext) -> TaskOutcome:
        try:
            payload = TransferPayloadV1.parse(context.task)
        except TransferPayloadError as exc:
            return TaskOutcome(
                status="failed",
                summary="Transfer payload validation failed",
                error_code="INVALID_TRANSFER_PAYLOAD",
                error_message=str(exc),
            )

        if await context.cancellation_requested():
            await asyncio.to_thread(self._mark_cancelled, payload.file_id)
            return TaskOutcome(
                status="cancelled",
                summary="Transfer cancelled before execution",
            )

        try:
            source = await asyncio.to_thread(self._load_source, payload)
        except Exception as exc:
            disposition = self._classify_failure(exc)
            if not isinstance(exc, TransferSourceNotFoundError):
                await asyncio.to_thread(
                    self._mark_failure,
                    payload.file_id,
                    disposition,
                    retry_exhausted=self._retry_exhausted(
                        context,
                        disposition,
                    ),
                )
            return self._outcome_for_failure(disposition)

        await asyncio.to_thread(self._mark_saving, source.file_id)
        provider: CloudDriveProvider | None = None
        operation_result: TransferOperationResult | None = None
        operation_error: Exception | None = None
        try:
            provider = self._provider_factory(
                source.provider_type,
                source.refresh_token,
                source.target_drive_id,
            )
            operation_result = await execute_transfer(
                provider,
                source.spec,
                cancellation_requested=context.cancellation_requested,
                provider_operation_id=context.task.provider_operation_id,
                record_write_intent=lambda: asyncio.to_thread(
                    self._record_write_intent, context.task.task_id
                ),
                record_provider_operation=lambda operation_id: asyncio.to_thread(
                    self._record_provider_operation,
                    context.task.task_id,
                    operation_id,
                ),
            )
        except Exception as exc:
            operation_error = exc

        token_persisted = True
        if provider is not None:
            try:
                await asyncio.to_thread(
                    self._persist_provider_token,
                    source.account_id,
                    provider,
                )
            except Exception:
                token_persisted = False

        if operation_result is not None:
            try:
                await asyncio.to_thread(
                    self._record_provider_success,
                    context.task.task_id,
                    operation_result,
                )
                await asyncio.to_thread(
                    self._mark_success,
                    source.file_id,
                    operation_result,
                )
            except Exception:
                return TaskOutcome(
                    status="retry",
                    summary="Remote transfer succeeded but local state was not persisted",
                    error_code="TRANSFER_STATE_PERSIST_FAILED",
                    error_message="local transfer state persistence failed",
                )
            if not token_persisted:
                return TaskOutcome(
                    status="retry",
                    summary="Remote transfer succeeded but rotated credential was not persisted",
                    error_code="CREDENTIAL_PERSIST_FAILED",
                    error_message="rotated provider credential persistence failed",
                )
            return self._success_outcome(provider, operation_result)

        if isinstance(operation_error, TransferCancelledError):
            await asyncio.to_thread(self._mark_cancelled, source.file_id)
            return TaskOutcome(
                status="cancelled",
                summary="Transfer cancelled before remote save",
            )

        if operation_error is None:
            operation_error = RuntimeError("transfer operation returned no result")
        if isinstance(operation_error, ProviderWriteUncertainError):
            await asyncio.to_thread(
                self._record_provider_uncertain,
                context.task.task_id,
            )
        disposition = self._classify_failure(operation_error)
        if not token_persisted:
            disposition = _FailureDisposition(
                status="retry",
                error_code="CREDENTIAL_PERSIST_FAILED",
                safe_message="rotated provider credential persistence failed",
            )

        if await context.cancellation_requested():
            await asyncio.to_thread(self._mark_cancelled, source.file_id)
            return TaskOutcome(
                status="cancelled",
                summary="Transfer cancelled after a failed provider operation",
            )

        await asyncio.to_thread(
            self._mark_failure,
            source.file_id,
            disposition,
            retry_exhausted=self._retry_exhausted(context, disposition),
        )
        return self._outcome_for_failure(disposition)

    def _load_source(self, payload: TransferPayloadV1) -> _TransferSource:
        with self._session_factory() as session:
            file = session.get(CloudFile, payload.file_id)
            if file is None or file.subscription_id != payload.subscription_id:
                raise TransferSourceNotFoundError(
                    "transfer source file or subscription was not found"
                )
            subscription = file.subscription
            account = subscription.cloud_account
            return _TransferSource(
                file_id=file.id,
                account_id=account.id,
                provider_type=account.provider,
                refresh_token=self._token_loader(account),
                target_drive_id=subscription.target_drive_id,
                spec=TransferSpec(
                    share_url=subscription.share_url,
                    share_password=decrypt_share_password(subscription),
                    target_path=subscription.target_path,
                    remote_file_id=file.remote_file_id,
                    parent_remote_file_id=file.parent_remote_file_id,
                    filename=file.filename,
                    relative_path=file.relative_path,
                    item_type=file.item_type,
                    size=file.size,
                    content_hash=file.content_hash,
                ),
            )

    def _mark_saving(self, file_id: int) -> None:
        with self._session_factory() as session, session.begin():
            file = self._require_file(session, file_id)
            file.status = "saving"
            file.last_error = None

    def _record_write_intent(self, task_id: int) -> None:
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TransferSourceNotFoundError("transfer task was not found")
            if task.provider_operation_id is not None:
                return
            task.provider_write_intent_at = task.provider_write_intent_at or self._clock()
            task.provider_operation_status = "intent"

    def _record_provider_operation(self, task_id: int, operation_id: str) -> None:
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TransferSourceNotFoundError("transfer task was not found")
            if task.provider_operation_id not in (None, operation_id):
                raise RuntimeError("transfer task already has another provider operation")
            task.provider_operation_id = operation_id
            task.provider_operation_status = "pending"

    def _record_provider_success(
        self,
        task_id: int,
        result: TransferOperationResult,
    ) -> None:
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                raise TransferSourceNotFoundError("transfer task was not found")
            if task.provider_operation_id is None:
                return
            task.provider_operation_status = "succeeded"
            task.provider_result = {
                "target_file_id": result.target_file_id,
                "target_path": result.target_path,
            }

    def _record_provider_uncertain(self, task_id: int) -> None:
        with self._session_factory() as session, session.begin():
            task = session.get(Task, task_id)
            if task is None:
                return
            if task.provider_operation_id is None:
                task.provider_operation_status = "uncertain"

    def _mark_success(
        self,
        file_id: int,
        result: TransferOperationResult,
    ) -> None:
        with self._session_factory() as session, session.begin():
            file = self._require_file(session, file_id)
            file.status = "saved"
            file.target_file_id = result.target_file_id
            file.target_path = result.target_path
            file.saved_at = self._clock()
            file.last_error = None

    def _mark_failure(
        self,
        file_id: int,
        disposition: _FailureDisposition,
        *,
        retry_exhausted: bool = False,
    ) -> None:
        with self._session_factory() as session, session.begin():
            file = self._require_file(session, file_id)
            file.status = (
                "failed"
                if disposition.status == "failed" or retry_exhausted
                else "pending"
            )
            file.last_error = disposition.safe_message

    @staticmethod
    def _retry_exhausted(
        context: TaskExecutionContext,
        disposition: _FailureDisposition,
    ) -> bool:
        return (
            disposition.status == "retry"
            and context.task.retry_count >= context.task.max_retries
        )

    def _mark_cancelled(self, file_id: int) -> None:
        with self._session_factory() as session, session.begin():
            file = session.get(CloudFile, file_id)
            if file is None:
                return
            file.status = "pending"
            file.last_error = "transfer cancelled"

    def _persist_provider_token(
        self,
        account_id: int,
        provider: CloudDriveProvider,
    ) -> None:
        with self._session_factory() as session, session.begin():
            account = session.get(CloudAccount, account_id)
            if account is None:
                raise TransferSourceNotFoundError("transfer cloud account was not found")
            self._token_persister(account, provider)

    @staticmethod
    def _require_file(session: Session, file_id: int) -> CloudFile:
        file = session.get(CloudFile, file_id)
        if file is None:
            raise TransferSourceNotFoundError("transfer source file was not found")
        return file

    @staticmethod
    def _success_outcome(
        provider: CloudDriveProvider,
        result: TransferOperationResult,
    ) -> TaskOutcome:
        request_count = getattr(provider, "request_count", None)
        metrics: dict[str, object] = {
            "already_existed": result.already_existed,
        }
        if isinstance(request_count, int):
            metrics["provider_request_count"] = request_count
        return TaskOutcome(
            status="success",
            summary=f"Transferred to {result.target_path}",
            metrics=metrics,
        )

    @staticmethod
    def _classify_failure(exc: Exception) -> _FailureDisposition:
        if isinstance(exc, TransferSourceNotFoundError):
            return _FailureDisposition(
                status="failed",
                error_code="TRANSFER_SOURCE_NOT_FOUND",
                safe_message="transfer source file, subscription, or account was not found",
            )
        if isinstance(exc, ProviderNotConfiguredError):
            return _FailureDisposition(
                status="failed",
                error_code=exc.code,
                safe_message="cloud-drive provider is not configured",
            )
        if isinstance(exc, ProviderCapabilityError):
            return _FailureDisposition(
                status="failed",
                error_code=exc.code,
                safe_message="cloud-drive provider does not support this transfer",
            )
        message = str(exc).lower()
        credential_value_error = isinstance(exc, ValueError) and "credential" in message
        credential_provider_error = isinstance(
            exc, ProviderRequestError
        ) and any(marker in message for marker in _CREDENTIAL_ERROR_MARKERS)
        if credential_value_error or credential_provider_error:
            return _FailureDisposition(
                status="waiting_credential",
                error_code="CREDENTIAL_INVALID",
                safe_message="cloud-drive credential is invalid or expired",
                blocked_reason="cloud-drive credential requires user action",
            )
        if isinstance(exc, ProviderRequestError):
            if isinstance(exc, ProviderWriteUncertainError):
                return _FailureDisposition(
                    status="failed",
                    error_code=exc.code,
                    safe_message=(
                        "cloud-drive write result is uncertain and requires reconciliation"
                    ),
                )
            safe_message = (
                str(exc)
                if exc.code.startswith("QUARK_")
                else "cloud-drive request failed and can be retried"
            )
            return _FailureDisposition(
                status="retry",
                error_code=exc.code,
                safe_message=safe_message,
            )
        if isinstance(exc, ProviderError):
            return _FailureDisposition(
                status="failed",
                error_code=exc.code,
                safe_message="cloud-drive provider configuration is invalid",
            )
        return _FailureDisposition(
            status="retry",
            error_code=getattr(exc, "code", "TRANSFER_EXECUTION_FAILED"),
            safe_message="cloud-drive transfer failed and can be retried",
        )

    @staticmethod
    def _outcome_for_failure(disposition: _FailureDisposition) -> TaskOutcome:
        if disposition.status == "waiting_credential":
            return TaskOutcome(
                status="waiting_credential",
                summary="Transfer is waiting for a valid cloud-drive credential",
                error_code=disposition.error_code,
                error_message=disposition.safe_message,
                blocked_reason=disposition.blocked_reason,
            )
        return TaskOutcome(
            status=disposition.status,
            summary=disposition.safe_message,
            error_code=disposition.error_code,
            error_message=disposition.safe_message,
        )
