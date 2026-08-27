from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.exceptions import ProviderError, ProviderWriteUncertainError
from app.providers.base import FolderRef
from app.providers.pan123.cli import _read_token_from_clipboard
from app.providers.pan123.provider import Pan123PrivateProvider

CONFIRMATION = "CREATE ONE TEST FOLDER"


@dataclass(frozen=True, slots=True)
class FolderProbeReport:
    submitted: bool
    completed: bool
    target_verified: bool


def _state_path(parent_id: str, name: str) -> Path:
    fingerprint = hashlib.sha256(f"{parent_id}\0{name}".encode()).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"mediasync-pan123-folder-{fingerprint}.json"


def _write_intent(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"status": "intent"}, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())


async def run_folder_probe(
    provider: Pan123PrivateProvider,
    *,
    parent_id: str,
    name: str,
    confirmed: bool,
) -> FolderProbeReport:
    if not parent_id.isdecimal():
        raise ValueError("123 parent folder ID must be numeric")
    parent = FolderRef(parent_id, "/")
    existing = await provider.find_target_item(parent, name)
    state_path = _state_path(parent_id, name)
    if existing is not None:
        if existing.item_type != "folder":
            raise ValueError("The target already exists and is not a folder")
        state_path.unlink(missing_ok=True)
        return FolderProbeReport(False, True, True)
    if state_path.exists():
        raise ProviderWriteUncertainError(
            f"An uncertain 123 folder intent exists at {state_path}; do not submit again"
        )
    if not confirmed:
        raise ValueError(f"Explicit confirmation {CONFIRMATION!r} is required")
    _write_intent(state_path)
    try:
        await provider.ensure_folder(parent, name)
    except ProviderWriteUncertainError:
        raise
    except Exception:
        state_path.unlink(missing_ok=True)
        raise
    state_path.unlink(missing_ok=True)
    return FolderProbeReport(True, True, True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and verify one non-replayed 123 Cloud Drive test folder."
    )
    parser.add_argument("--parent-folder-id", default="0")
    parser.add_argument("--folder-name", default="MediaSync测试")
    parser.add_argument("--token-clipboard", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = (
        _read_token_from_clipboard()
        if args.token_clipboard
        else getpass.getpass("123 Access Token (hidden, not saved): ")
    ).strip()
    confirmation = input(f"Type {CONFIRMATION!r} to perform one remote write: ").strip()
    try:
        report = asyncio.run(
            run_folder_probe(
                Pan123PrivateProvider(token),
                parent_id=args.parent_folder_id,
                name=args.folder_name,
                confirmed=confirmation == CONFIRMATION,
            )
        )
        payload: dict[str, object] = {
            "provider": "pan123",
            "mode": "private_api_folder_probe",
            "credentials_persisted": False,
            "remote_write_performed": report.submitted,
            "checks": asdict(report),
        }
        exit_code = 0
    except (ProviderError, ValueError) as exc:
        message = str(exc).replace(token, "[redacted]") if token else str(exc)
        payload = {
            "provider": "pan123",
            "mode": "private_api_folder_probe",
            "credentials_persisted": False,
            "error": {
                "code": getattr(exc, "code", "PAN123_FOLDER_PROBE_FAILED"),
                "message": message,
            },
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
