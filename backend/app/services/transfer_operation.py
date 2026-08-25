from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.core.exceptions import (
    ProviderOperationPendingError,
    ProviderWriteUncertainError,
)
from app.providers.base import (
    CloudDriveProvider,
    FolderRef,
    RemoteItem,
    ResumableSaveProvider,
    SaveResult,
)

CancellationProbe = Callable[[], Awaitable[bool]]
WriteIntentRecorder = Callable[[], Awaitable[None]]
OperationRecorder = Callable[[str], Awaitable[None]]


class TransferCancelledError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransferSpec:
    share_url: str
    share_password: str | None
    target_path: str
    remote_file_id: str
    parent_remote_file_id: str | None
    filename: str
    relative_path: str
    item_type: str
    size: int | None
    content_hash: str | None


@dataclass(frozen=True)
class TransferOperationResult:
    target_file_id: str
    target_path: str
    already_existed: bool


async def _never_cancelled() -> bool:
    return False


async def _ignore_intent() -> None:
    return None


async def _ignore_operation(_operation_id: str) -> None:
    return None


async def ensure_target_folder(
    provider: CloudDriveProvider,
    root_path: str,
    relative_path: str,
    *,
    cancellation_requested: CancellationProbe = _never_cancelled,
) -> FolderRef:
    target = FolderRef(folder_id="root", path="/")
    full_path = PurePosixPath(root_path) / PurePosixPath(relative_path).parent
    for part in full_path.parts:
        if part in {"", ".", "/"}:
            continue
        if await cancellation_requested():
            raise TransferCancelledError("transfer cancelled before target folder preparation")
        target = await provider.ensure_folder(target, part)
    return target


async def execute_transfer(
    provider: CloudDriveProvider,
    spec: TransferSpec,
    *,
    cancellation_requested: CancellationProbe = _never_cancelled,
    provider_operation_id: str | None = None,
    record_write_intent: WriteIntentRecorder = _ignore_intent,
    record_provider_operation: OperationRecorder = _ignore_operation,
) -> TransferOperationResult:
    if await cancellation_requested():
        raise TransferCancelledError("transfer cancelled before provider access")

    share = await provider.resolve_share(spec.share_url, spec.share_password)
    target = await ensure_target_folder(
        provider,
        spec.target_path,
        spec.relative_path,
        cancellation_requested=cancellation_requested,
    )
    if provider_operation_id is None:
        existing = await provider.find_target_item(target, spec.filename)
        if existing is not None:
            return TransferOperationResult(
                target_file_id=existing.remote_file_id,
                target_path=str(PurePosixPath(target.path) / spec.filename),
                already_existed=True,
            )

    if await cancellation_requested():
        raise TransferCancelledError("transfer cancelled before remote save")
    source = RemoteItem(
        remote_file_id=spec.remote_file_id,
        parent_id=spec.parent_remote_file_id,
        filename=spec.filename,
        item_type=spec.item_type,
        size=spec.size,
        content_hash=spec.content_hash,
    )
    if isinstance(provider, ResumableSaveProvider):
        operation_id = provider_operation_id
        if operation_id is None:
            await record_write_intent()
            operation_id = await provider.start_save_shared_item(share, source, target)
            try:
                await record_provider_operation(operation_id)
            except Exception as exc:
                raise ProviderWriteUncertainError(
                    "cloud-drive operation was accepted but its ID was not persisted"
                ) from exc
        operation = await provider.query_save_operation(operation_id)
        if not operation.completed:
            raise ProviderOperationPendingError(
                "cloud-drive save operation is still pending"
            )
        if len(operation.target_file_ids) != 1:
            raise ProviderOperationPendingError(
                "cloud-drive save result requires manual reconciliation"
            )
        result = SaveResult(
            target_file_id=operation.target_file_ids[0],
            target_path=str(PurePosixPath(target.path) / spec.filename),
        )
    else:
        result = await provider.save_shared_item(share, source, target)
    return TransferOperationResult(
        target_file_id=result.target_file_id,
        target_path=result.target_path,
        already_existed=False,
    )
