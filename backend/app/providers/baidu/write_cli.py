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
from pathlib import Path

from app.core.exceptions import (
    ProviderError,
    ProviderOperationPendingError,
    ProviderWriteUncertainError,
)
from app.providers.baidu.share_cli import _read_cookie_from_clipboard
from app.providers.baidu.share_probe import BaiduShareUpstreamChangedError, _safe_int
from app.providers.baidu.write_client import BaiduWriteClient, normalize_target_path

CONFIRMATION = "WRITE ONE BAIDU TEST ITEM"


@dataclass(frozen=True, slots=True)
class WriteProbeReport:
    submitted: bool
    resumed: bool
    completed: bool
    target_verified: bool


def _items(payload: dict[str, object], stage: str) -> list[dict[str, object]]:
    if stage == "share":
        data = payload.get("data")
        raw_items = data.get("list") if isinstance(data, dict) else None
    else:
        raw_items = payload.get("list")
    if not isinstance(raw_items, list) or any(not isinstance(item, dict) for item in raw_items):
        raise BaiduShareUpstreamChangedError(f"Baidu {stage} response contained invalid items")
    return raw_items


def _state_path(share_id: str, source_id: int, target_path: str) -> Path:
    fingerprint = hashlib.sha256(
        f"{share_id}\0{source_id}\0{target_path}".encode()
    ).hexdigest()[:20]
    return Path(tempfile.gettempdir()) / f"mediasync-baidu-write-{fingerprint}.json"


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
        raise ValueError("Baidu write probe state is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Baidu write probe state is invalid")
    return payload


async def run_write_probe(
    client: BaiduWriteClient,
    *,
    share_url: str,
    share_password: str,
    target_path: str,
    confirmed: bool,
    poll_attempts: int = 30,
    poll_interval: float = 1.0,
) -> WriteProbeReport:
    target_path = normalize_target_path(target_path)
    await client.probe_account()
    share_id, _password, share_payload = await client.fetch_share_page(
        share_url,
        password=share_password,
        page_size=2,
    )
    source_items = _items(share_payload, "share")
    if len(source_items) != 1:
        raise ValueError("The write probe requires a share containing exactly one top-level item")
    source = source_items[0]
    source_id = _safe_int(source.get("fs_id"))
    source_name = source.get("server_filename")
    source_size = _safe_int(source.get("size"))
    if source_id is None or source_id <= 0:
        raise ValueError("Baidu share item has no valid fs_id")
    if not isinstance(source_name, str) or not source_name:
        raise ValueError("Baidu share item has no valid server_filename")

    state_path = _state_path(share_id, source_id, target_path)
    state = _read_state(state_path)
    submitted = False
    resumed = state is not None
    if state is None:
        target_payload = await client.fetch_target_page(target_path)
        target_items = _items(target_payload, "target")
        if any(item.get("server_filename") == source_name for item in target_items):
            raise ValueError("The target folder already contains an item with the same name")
        if not confirmed:
            raise ValueError(f"Explicit confirmation {CONFIRMATION!r} is required")
        share_data = share_payload.get("data")
        if not isinstance(share_data, dict):
            raise BaiduShareUpstreamChangedError("Baidu share response contained no data object")
        _write_state(state_path, {"status": "intent"})
        try:
            await client.save_share_item(
                share_url=share_url,
                share_data=share_data,
                source=source,
                target_path=target_path,
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
            f"An uncertain Baidu write intent exists at {state_path}; do not submit again"
        )
    elif state.get("status") != "submitted":
        raise ValueError("Baidu write probe state is invalid")

    verified = await client.wait_for_target_item(
        target_path,
        source_name=source_name,
        source_size=source_size,
        poll_attempts=poll_attempts,
        poll_interval=poll_interval,
    )
    if verified:
        state_path.unlink(missing_ok=True)
        return WriteProbeReport(
            submitted=submitted,
            resumed=resumed,
            completed=True,
            target_verified=True,
        )
    raise ProviderOperationPendingError(
        f"Baidu write is still pending; rerun the same command to verify from {state_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Perform one non-replaying Baidu share-transfer probe."
    )
    parser.add_argument("--target-path", required=True)
    parser.add_argument("--cookie-clipboard", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    cookie = (
        _read_cookie_from_clipboard()
        if args.cookie_clipboard
        else getpass.getpass("Baidu BDUSS or Cookie (hidden, not saved): ")
    ).strip()
    print("Cookie 已接收（仅在当前进程内使用，不保存）。", file=sys.stderr, flush=True)
    share_url = input("Single-item Baidu share URL: ").strip()
    share_password = getpass.getpass("Share password (hidden, optional): ").strip()
    confirmation = input(f"Type {CONFIRMATION!r} to perform one remote write: ").strip()
    sensitive_values = (cookie, share_password)
    try:
        report = asyncio.run(
            run_write_probe(
                BaiduWriteClient(cookie),
                share_url=share_url,
                share_password=share_password,
                target_path=args.target_path,
                confirmed=confirmation == CONFIRMATION,
            )
        )
        payload: dict[str, object] = {
            "provider": "baidu",
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
            "provider": "baidu",
            "mode": "private_api_write_probe",
            "credentials_persisted": False,
            "error": {
                "code": getattr(exc, "code", "BAIDU_WRITE_PROBE_FAILED"),
                "message": message,
            },
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
