from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from app.core.exceptions import (
    ProviderError,
    ProviderOperationPendingError,
    ProviderWriteUncertainError,
)
from app.providers.baidu.cli import _read_token_from_clipboard
from app.providers.baidu.open_write_client import BaiduOpenWriteClient
from app.providers.baidu.write_client import normalize_target_path

CONFIRMATION = "CREATE ONE BAIDU TEST FOLDER"


@dataclass(frozen=True, slots=True)
class FolderProbeReport:
    submitted: bool
    resumed: bool
    completed: bool
    target_verified: bool


def _items(payload: dict[str, object]) -> list[dict[str, object]]:
    raw_items = payload.get("list")
    if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
        raise ValueError("Baidu directory listing contained invalid items")
    return raw_items


def _state_path(target_path: str) -> Path:
    fingerprint = hashlib.sha256(target_path.encode()).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"mediasync-baidu-folder-{fingerprint}.json"


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
        raise ValueError("Baidu folder probe state is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Baidu folder probe state is invalid")
    return payload


async def run_folder_probe(
    client: BaiduOpenWriteClient,
    *,
    target_path: str,
    confirmed: bool,
    poll_attempts: int = 10,
    poll_interval: float = 1.0,
) -> FolderProbeReport:
    target_path = normalize_target_path(target_path)
    if target_path == "/":
        raise ValueError("The folder probe target cannot be root")
    target = PurePosixPath(target_path)
    parent_path = str(target.parent)
    folder_name = target.name
    await client.probe_account()
    state_path = _state_path(target_path)
    state = _read_state(state_path)
    submitted = False
    resumed = state is not None
    if state is None:
        parent_payload = await client.fetch_directory(parent_path)
        if any(item.get("server_filename") == folder_name for item in _items(parent_payload)):
            raise ValueError("The target folder already exists")
        if not confirmed:
            raise ValueError(f"Explicit confirmation {CONFIRMATION!r} is required")
        _write_state(state_path, {"status": "intent"})
        try:
            await client.create_folder(target_path)
        except ProviderWriteUncertainError:
            raise
        except Exception:
            state_path.unlink(missing_ok=True)
            raise
        _write_state(state_path, {"status": "submitted"})
        submitted = True
    elif state.get("status") == "intent":
        raise ProviderWriteUncertainError(
            f"An uncertain Baidu folder intent exists at {state_path}; do not submit again"
        )
    elif state.get("status") != "submitted":
        raise ValueError("Baidu folder probe state is invalid")

    for attempt in range(poll_attempts):
        parent_payload = await client.fetch_directory(parent_path)
        for item in _items(parent_payload):
            if item.get("server_filename") == folder_name and item.get("isdir") in {1, "1"}:
                state_path.unlink(missing_ok=True)
                return FolderProbeReport(
                    submitted=submitted,
                    resumed=resumed,
                    completed=True,
                    target_verified=True,
                )
        if attempt + 1 < poll_attempts:
            await asyncio.sleep(poll_interval)
    raise ProviderOperationPendingError(
        f"Baidu folder creation is still pending; rerun from {state_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and verify one Baidu Netdisk test folder through OpenAPI."
    )
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--token-clipboard", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = (
        _read_token_from_clipboard()
        if args.token_clipboard
        else getpass.getpass("Baidu Access Token (hidden, not saved): ")
    ).strip()
    print("Access Token 已接收（仅在当前进程内使用，不保存）。", file=sys.stderr, flush=True)
    confirmation = input(f"Type {CONFIRMATION!r} to create one remote folder: ").strip()
    try:
        report = asyncio.run(
            run_folder_probe(
                BaiduOpenWriteClient(token),
                target_path=args.target_path,
                confirmed=confirmation == CONFIRMATION,
            )
        )
        payload: dict[str, object] = {
            "provider": "baidu",
            "mode": "official_open_api_folder_probe",
            "credentials_persisted": False,
            "remote_write_performed": True,
            "checks": asdict(report),
        }
        exit_code = 0
    except (ProviderError, ValueError) as exc:
        message = str(exc).replace(token, "[redacted]") if token else str(exc)
        payload = {
            "provider": "baidu",
            "mode": "official_open_api_folder_probe",
            "credentials_persisted": False,
            "error": {
                "code": getattr(exc, "code", "BAIDU_FOLDER_PROBE_FAILED"),
                "message": message,
            },
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
