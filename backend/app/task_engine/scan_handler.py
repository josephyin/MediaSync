from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from app.core.exceptions import (
    ProviderCapabilityError,
    ProviderError,
    ProviderNotConfiguredError,
    ProviderRequestError,
)
from app.models import CloudAccount, Subscription
from app.providers import get_provider
from app.providers.base import CloudDriveProvider
from app.services.account_service import get_decrypted_token, persist_provider_token
from app.services.scan_service import (
    ScanCancelledError,
    ScanDomainResult,
    execute_scan_domain,
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


class ScanPayloadError(ValueError):
    pass


class ScanSourceNotFoundError(LookupError):
    pass


class ScanSubscriptionDisabledError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanPayloadV1:
    subscription_id: int
    force_full: bool

    @classmethod
    def parse(cls, invocation: TaskInvocation) -> ScanPayloadV1:
        if invocation.task_type != "scan":
            raise ScanPayloadError("scan handler requires task type 'scan'")
        if invocation.payload_version != 1:
            raise ScanPayloadError(
                f"unsupported scan payload version {invocation.payload_version}"
            )
        if invocation.subscription_id is None:
            raise ScanPayloadError("scan payload v1 requires subscription_id")
        unknown_fields = set(invocation.payload) - {"force_full"}
        if unknown_fields:
            raise ScanPayloadError("scan payload v1 contains unknown fields")
        force_full = invocation.payload.get("force_full", False)
        if not isinstance(force_full, bool):
            raise ScanPayloadError("scan payload v1 force_full must be boolean")
        return cls(
            subscription_id=invocation.subscription_id,
            force_full=force_full,
        )


@dataclass(frozen=True)
class _FailureDisposition:
    status: FailureStatus
    error_code: str
    safe_message: str
    blocked_reason: str | None = None


class ScanTaskHandler:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        provider_factory: ProviderFactory = get_provider,
        token_loader: TokenLoader = get_decrypted_token,
        token_persister: TokenPersister = persist_provider_token,
    ) -> None:
        self._session_factory = session_factory
        self._provider_factory = provider_factory
        self._token_loader = token_loader
        self._token_persister = token_persister

    async def __call__(self, context: TaskExecutionContext) -> TaskOutcome:
        try:
            payload = ScanPayloadV1.parse(context.task)
        except ScanPayloadError as exc:
            return TaskOutcome(
                status="failed",
                summary="Scan payload validation failed",
                error_code="INVALID_SCAN_PAYLOAD",
                error_message=str(exc),
            )

        if await context.cancellation_requested():
            return TaskOutcome(
                status="cancelled",
                summary="Scan cancelled before execution",
            )

        with self._session_factory() as session:
            subscription = session.get(Subscription, payload.subscription_id)
            if subscription is None:
                return self._outcome_for_failure(
                    self._classify_failure(
                        ScanSourceNotFoundError("scan subscription was not found")
                    )
                )
            if not subscription.enabled:
                return self._outcome_for_failure(
                    self._classify_failure(
                        ScanSubscriptionDisabledError("scan subscription is disabled")
                    )
                )

            provider: CloudDriveProvider | None = None
            try:
                account = subscription.cloud_account
                provider = self._provider_factory(
                    account.provider,
                    self._token_loader(account),
                    None,
                )
                subscription.status = "scanning"
                subscription.last_error = None
                session.commit()
                result = await execute_scan_domain(
                    session,
                    subscription,
                    provider,
                    force_full=payload.force_full,
                    cancellation_requested=context.cancellation_requested,
                )
            except ScanCancelledError:
                session.rollback()
                self._mark_recoverable(session, subscription.id, "scan cancelled")
                self._persist_token_if_available(session, subscription.id, provider)
                return TaskOutcome(
                    status="cancelled",
                    summary="Scan cancelled at a safe traversal boundary",
                )
            except Exception as exc:
                session.rollback()
                disposition = self._classify_failure(exc)
                self._mark_failure(session, subscription.id, disposition)
                token_persisted = self._persist_token_if_available(
                    session,
                    subscription.id,
                    provider,
                )
                if not token_persisted:
                    disposition = _FailureDisposition(
                        status="retry",
                        error_code="CREDENTIAL_PERSIST_FAILED",
                        safe_message="rotated provider credential persistence failed",
                    )
                    self._mark_failure(session, subscription.id, disposition)
                return self._outcome_for_failure(disposition)

            if not self._persist_token_if_available(
                session,
                subscription.id,
                provider,
            ):
                disposition = _FailureDisposition(
                    status="retry",
                    error_code="CREDENTIAL_PERSIST_FAILED",
                    safe_message="rotated provider credential persistence failed",
                )
                self._mark_failure(session, subscription.id, disposition)
                return TaskOutcome(
                    status="retry",
                    summary="Scan completed but rotated credential was not persisted",
                    error_code=disposition.error_code,
                    error_message=disposition.safe_message,
                )
            return self._success_outcome(provider, result)

    @staticmethod
    def _mark_recoverable(
        session: Session,
        subscription_id: int,
        safe_message: str,
    ) -> None:
        subscription = session.get(Subscription, subscription_id)
        if subscription is None:
            return
        subscription.status = "active" if subscription.enabled else "disabled"
        subscription.last_error = safe_message
        session.commit()

    @staticmethod
    def _mark_failure(
        session: Session,
        subscription_id: int,
        disposition: _FailureDisposition,
    ) -> None:
        subscription = session.get(Subscription, subscription_id)
        if subscription is None:
            return
        subscription.status = "error"
        subscription.last_error = disposition.safe_message
        session.commit()

    def _persist_token_if_available(
        self,
        session: Session,
        subscription_id: int,
        provider: CloudDriveProvider | None,
    ) -> bool:
        if provider is None:
            return True
        try:
            subscription = session.get(Subscription, subscription_id)
            if subscription is None:
                return False
            self._token_persister(subscription.cloud_account, provider)
            session.commit()
        except Exception:
            session.rollback()
            return False
        return True

    @staticmethod
    def _success_outcome(
        provider: CloudDriveProvider,
        result: ScanDomainResult,
    ) -> TaskOutcome:
        metrics: dict[str, object] = {
            "full_scan": result.full_scan,
            "folders_scanned": result.scan.folders_scanned,
            "files_discovered": result.scan.discovered,
            "checkpoint_count": result.checkpoint_count,
        }
        request_count = getattr(provider, "request_count", None)
        if isinstance(request_count, int):
            metrics["provider_request_count"] = request_count
        mode = "full" if result.full_scan else "incremental"
        return TaskOutcome(
            status="success",
            summary=(
                f"{mode} scan completed: {result.scan.folders_scanned} folders, "
                f"{result.scan.discovered} new items"
            ),
            metrics=metrics,
        )

    @staticmethod
    def _classify_failure(exc: Exception) -> _FailureDisposition:
        if isinstance(exc, ScanSourceNotFoundError):
            return _FailureDisposition(
                status="failed",
                error_code="SCAN_SUBSCRIPTION_NOT_FOUND",
                safe_message="scan subscription was not found",
            )
        if isinstance(exc, ScanSubscriptionDisabledError):
            return _FailureDisposition(
                status="failed",
                error_code="SCAN_SUBSCRIPTION_DISABLED",
                safe_message="scan subscription is disabled",
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
                safe_message="cloud-drive provider does not support share scanning",
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
            return _FailureDisposition(
                status="retry",
                error_code=exc.code,
                safe_message="cloud-drive scan request failed and can be retried",
            )
        if isinstance(exc, ProviderError):
            return _FailureDisposition(
                status="failed",
                error_code=exc.code,
                safe_message="cloud-drive provider configuration is invalid",
            )
        return _FailureDisposition(
            status="retry",
            error_code=getattr(exc, "code", "SCAN_EXECUTION_FAILED"),
            safe_message="cloud-drive scan failed and can be retried",
        )

    @staticmethod
    def _outcome_for_failure(disposition: _FailureDisposition) -> TaskOutcome:
        if disposition.status == "waiting_credential":
            return TaskOutcome(
                status="waiting_credential",
                summary="Scan is waiting for a valid cloud-drive credential",
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
