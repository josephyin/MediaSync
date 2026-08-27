from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.exceptions import (
    ProviderError,
    ProviderOperationPendingError,
    ProviderWriteUncertainError,
)
from app.providers.pan123.cli import _read_token_from_clipboard
from app.providers.pan123.readonly_probe import Pan123UpstreamChangedError
from app.providers.pan123.write_client import Pan123WriteClient

CONFIRMATION = "WRITE ONE TEST ITEM"


@dataclass(frozen=True, slots=True)
class WriteProbeReport:
    submitted: bool
    resumed: bool
    completed: bool
    target_verified: bool
    login_uuid_generated: bool


def _items(payload: dict[str, object], stage: str) -> list[dict[str, object]]:
    data = payload.get("data")
    raw_items = data.get("InfoList") if isinstance(data, dict) else None
    if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
        raise Pan123UpstreamChangedError(
            f"123 Cloud Drive {stage} response contained invalid items"
        )
    return raw_items


def _state_path(share_key: str, source_id: int, target_folder_id: str) -> Path:
    fingerprint = hashlib.sha256(
        f"{share_key}\0{source_id}\0{target_folder_id}".encode()
    ).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"mediasync-pan123-write-{fingerprint}.json"


def _write_state(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("123 write probe state is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("123 write probe state is invalid")
    return payload


async def run_write_probe(
    client: Pan123WriteClient,
    *,
    share_url: str,
    share_password: str,
    target_folder_id: str,
    login_uuid: str,
    login_uuid_generated: bool,
    confirmed: bool,
    poll_attempts: int = 30,
    poll_interval: float = 1.0,
) -> WriteProbeReport:
    await client.probe_account()
    share_key, password, share_payload = await client.fetch_share_page(
        share_url,
        share_password=share_password,
        page_size=2,
    )
    source_items = _items(share_payload, "share")
    if len(source_items) != 1:
        raise ValueError("The write probe requires a share containing exactly one top-level item")
    source = source_items[0]
    source_id = source.get("FileId")
    source_name = source.get("FileName")
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("123 share item has no valid FileId")
    if not isinstance(source_name, str) or not source_name:
        raise ValueError("123 share item has no valid FileName")
    if not target_folder_id.isdecimal():
        raise ValueError("123 target folder ID must be numeric")

    state_path = _state_path(share_key, source_id, target_folder_id)
    state = _read_state(state_path)
    submitted = False
    resumed = state is not None
    if state is None:
        target_payload = await client.fetch_drive_page(target_folder_id, page_size=100)
        if any(item.get("FileName") == source_name for item in _items(target_payload, "target")):
            raise ValueError("The target folder already contains an item with the same name")
        if not confirmed:
            raise ValueError(f"Explicit confirmation {CONFIRMATION!r} is required")
        _write_state(state_path, {"status": "intent"})
        try:
            await client.save_share_item(
                share_key=share_key,
                share_password=password,
                source=source,
                target_folder_id=target_folder_id,
                login_uuid=login_uuid,
            )
        except ProviderWriteUncertainError:
            raise
        except Exception:
            state_path.unlink(missing_ok=True)
            raise
        _write_state(state_path, {"status": "submitted"})
        submitted = True
    elif state.get("status") == "intent":
        raise ProviderWriteUncertainError(
            f"An uncertain 123 write intent exists at {state_path}; do not submit again"
        )
    elif state.get("status") != "submitted":
        raise ValueError("123 write probe state is invalid")

    for _attempt in range(poll_attempts):
        target_payload = await client.fetch_drive_page(target_folder_id, page_size=100)
        if any(item.get("FileName") == source_name for item in _items(target_payload, "target")):
            state_path.unlink(missing_ok=True)
            return WriteProbeReport(
                submitted=submitted,
                resumed=resumed,
                completed=True,
                target_verified=True,
                login_uuid_generated=login_uuid_generated,
            )
        await asyncio.sleep(poll_interval)
    raise ProviderOperationPendingError(
        f"123 write is still pending; rerun the same command to verify from {state_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Perform one non-replaying 123 Cloud Drive share-save probe."
    )
    parser.add_argument("--target-folder-id", default="0")
    parser.add_argument("--token-clipboard", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = (
        _read_token_from_clipboard()
        if args.token_clipboard
        else getpass.getpass("123 Access Token (hidden, not saved): ")
    ).strip()
    print("Token 已接收（仅在当前进程内使用，不保存）。", file=sys.stderr, flush=True)
    login_uuid_input = getpass.getpass(
        "LoginUuid (hidden, optional; blank generates a process-only UUID): "
    ).strip()
    login_uuid_generated = not login_uuid_input
    login_uuid = login_uuid_input or uuid.uuid4().hex
    share_url = input("Single-item 123 share URL: ").strip()
    share_password = getpass.getpass("Share password (hidden, optional): ").strip()
    confirmation = input(f"Type {CONFIRMATION!r} to perform one remote write: ").strip()
    sensitive_values = (token, login_uuid_input, share_password)
    try:
        report = asyncio.run(
            run_write_probe(
                Pan123WriteClient(token),
                share_url=share_url,
                share_password=share_password,
                target_folder_id=args.target_folder_id,
                login_uuid=login_uuid,
                login_uuid_generated=login_uuid_generated,
                confirmed=confirmation == CONFIRMATION,
            )
        )
        payload: dict[str, object] = {
            "provider": "pan123",
            "mode": "private_api_write_probe",
            "credentials_persisted": False,
            "remote_write_performed": True,
            "checks": asdict(report),
        }
        exit_code = 0
    except (ProviderError, ValueError) as exc:
        message = str(exc)
        for value in sensitive_values:
            if value:
                message = message.replace(value, "[redacted]")
        payload = {
            "provider": "pan123",
            "mode": "private_api_write_probe",
            "credentials_persisted": False,
            "error": {
                "code": getattr(exc, "code", "PAN123_WRITE_PROBE_FAILED"),
                "message": message,
            },
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
