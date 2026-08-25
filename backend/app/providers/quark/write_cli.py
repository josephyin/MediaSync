from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from app.core.exceptions import (
    ProviderError,
    ProviderOperationPendingError,
    ProviderWriteUncertainError,
)
from app.providers.base import FolderRef, RemoteItem, ShareInfo
from app.providers.quark.provider import QuarkPrivateProvider

CONFIRMATION = "WRITE ONE TEST ITEM"


class WriteProbeProvider(Protocol):
    async def resolve_share(self, share_url: str, password: str | None = None) -> ShareInfo: ...

    async def list_share_items(
        self, share: ShareInfo, parent_id: str, marker: str | None = None
    ): ...

    async def resolve_target_path(self, path: str) -> FolderRef: ...

    async def find_target_item(
        self, target: FolderRef, name: str
    ) -> RemoteItem | None: ...

    async def start_save_shared_item(
        self, share: ShareInfo, source: RemoteItem, target: FolderRef
    ) -> str: ...

    async def query_save_operation(self, operation_id: str): ...

    def consume_refresh_token_update(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class WriteProbeReport:
    submitted: bool
    resumed: bool
    completed: bool
    target_verified: bool
    cookie_rotated_in_memory: bool


def _state_path(share: ShareInfo, source: RemoteItem, target: FolderRef) -> Path:
    fingerprint = hashlib.sha256(
        f"{share.share_key}\0{source.remote_file_id}\0{target.folder_id}".encode()
    ).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"mediasync-quark-write-{fingerprint}.json"


def _write_state(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Quark write probe state is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Quark write probe state is invalid")
    return payload


async def run_write_probe(
    provider: WriteProbeProvider,
    *,
    share_url: str,
    share_password: str | None,
    target_path: str,
    confirmed: bool,
    poll_attempts: int = 30,
    poll_interval: float = 0.5,
) -> WriteProbeReport:
    share = await provider.resolve_share(share_url, share_password)
    page = await provider.list_share_items(share, share.root_folder_id)
    if len(page.items) != 1 or page.next_marker is not None:
        raise ValueError("The write probe requires a share containing exactly one top-level item")
    source = page.items[0]
    target = await provider.resolve_target_path(target_path)
    state_path = _state_path(share, source, target)
    state = _read_state(state_path)
    operation_id: str | None = None
    resumed = False
    submitted = False
    if state is not None:
        raw_operation_id = state.get("operation_id")
        if not isinstance(raw_operation_id, str) or not raw_operation_id:
            raise ValueError(
                f"An uncertain write intent exists at {state_path}; do not submit again"
            )
        operation_id = raw_operation_id
        resumed = True
    else:
        existing = await provider.find_target_item(target, source.filename)
        if existing is not None:
            raise ValueError("The target folder already contains an item with the same name")
        if not confirmed:
            raise ValueError(f"Explicit confirmation {CONFIRMATION!r} is required")
        _write_state(state_path, {"status": "intent"})
        try:
            operation_id = await provider.start_save_shared_item(share, source, target)
        except ProviderWriteUncertainError:
            raise
        except Exception:
            state_path.unlink(missing_ok=True)
            raise
        _write_state(
            state_path,
            {"status": "pending", "operation_id": operation_id},
        )
        submitted = True

    for _attempt in range(poll_attempts):
        operation = await provider.query_save_operation(operation_id)
        if operation.completed:
            if len(operation.target_file_ids) != 1:
                raise ValueError("The completed write returned an ambiguous target result")
            saved = await provider.find_target_item(target, source.filename)
            verified = (
                saved is not None
                and saved.remote_file_id == operation.target_file_ids[0]
            )
            if not verified:
                raise ValueError("The completed write could not be verified in the target folder")
            state_path.unlink(missing_ok=True)
            return WriteProbeReport(
                submitted=submitted,
                resumed=resumed,
                completed=True,
                target_verified=True,
                cookie_rotated_in_memory=(
                    provider.consume_refresh_token_update() is not None
                ),
            )
        await asyncio.sleep(poll_interval)
    raise ProviderOperationPendingError(
        f"Quark write is still pending; rerun the same command to resume from {state_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Perform one resumable, non-overwriting Quark share-save probe."
    )
    parser.add_argument("--target-path", default="/MediaSync测试")
    parser.add_argument(
        "--cookie-clipboard",
        action="store_true",
        help="read the Cookie directly from the local macOS clipboard",
    )
    return parser


def _read_cookie_from_clipboard() -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/pbpaste"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Unable to read the local macOS clipboard") from exc
    return result.stdout


def main() -> int:
    args = build_parser().parse_args()
    cookie = (
        _read_cookie_from_clipboard()
        if args.cookie_clipboard
        else getpass.getpass("Quark Cookie (hidden, not saved): ")
    ).strip()
    print(
        "Cookie 已接收（仅在当前进程内使用，不保存）。",
        file=sys.stderr,
        flush=True,
    )
    share_url = input("Single-item Quark share URL: ").strip()
    share_password = getpass.getpass("Share password (hidden, optional): ").strip() or None
    confirmation = input(f"Type {CONFIRMATION!r} to perform one remote write: ").strip()
    secrets = (cookie, share_password or "")
    try:
        if not cookie:
            raise ValueError("Quark Cookie is required")
        report = asyncio.run(
            run_write_probe(
                QuarkPrivateProvider(cookie, page_size=2, max_retries=1),
                share_url=share_url,
                share_password=share_password,
                target_path=args.target_path,
                confirmed=confirmation == CONFIRMATION,
            )
        )
        payload: dict[str, object] = {
            "provider": "quark",
            "mode": "private_api_write_probe",
            "credentials_persisted": False,
            "remote_write_performed": True,
            "checks": asdict(report),
        }
        exit_code = 0
    except (ProviderError, ValueError) as exc:
        message = str(exc)
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
        payload = {
            "provider": "quark",
            "mode": "private_api_write_probe",
            "credentials_persisted": False,
            "error": {
                "code": getattr(exc, "code", "QUARK_WRITE_PROBE_FAILED"),
                "message": message,
            },
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
