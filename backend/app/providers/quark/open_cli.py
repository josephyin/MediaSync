from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from app.core.exceptions import ProviderError
from app.providers.quark.open_provider import (
    DEFAULT_OPENLIST_TOKEN_URL,
    QuarkOpenProvider,
)


@dataclass(frozen=True, slots=True)
class OpenProbeReport:
    account_accepted: bool
    default_drive_id_present: bool
    root_item_count: int
    root_has_more: bool
    rotated_refresh_token: bool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Quark OpenList credentials without saving or printing credentials, "
            "account identity, file names, or file IDs."
        )
    )
    parser.add_argument(
        "--token-url",
        default=DEFAULT_OPENLIST_TOKEN_URL,
        help="Trusted OpenList /quarkyun/renewapi HTTPS endpoint",
    )
    return parser


async def _run(
    refresh_token: str,
    app_id: str,
    sign_key: str,
    token_url: str,
    *,
    provider_factory=QuarkOpenProvider,
) -> OpenProbeReport:
    provider = provider_factory(
        refresh_token=refresh_token,
        app_id=app_id,
        sign_key=sign_key,
        oauth_token_url=token_url,
    )
    print("[1/2] 正在验证 Quark OpenAPI 账号…", file=sys.stderr)
    profile = await provider.validate_account()
    print("[2/2] 正在读取 OpenAPI 根目录第一页…", file=sys.stderr)
    root = await provider.resolve_target_path("/")
    page = await provider.list_target_items(root)
    rotated = provider.consume_refresh_token_update() is not None
    return OpenProbeReport(
        account_accepted=bool(profile.user_id),
        default_drive_id_present=bool(profile.default_drive_id),
        root_item_count=len(page.items),
        root_has_more=page.next_marker is not None,
        rotated_refresh_token=rotated,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    refresh_token = getpass.getpass("OpenList Refresh Token (hidden, not saved): ").strip()
    app_id = getpass.getpass("Quark AppID (hidden, required): ").strip()
    sign_key = getpass.getpass("Quark SignKey (hidden, required): ").strip()
    secrets = (refresh_token, app_id, sign_key)
    if not refresh_token or not app_id or not sign_key:
        print(
            json.dumps(
                {
                    "provider": "quark_open",
                    "persisted": False,
                    "error": {
                        "code": "QUARK_OPEN_INPUT_INVALID",
                        "message": "Refresh Token, AppID, and SignKey are required",
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    try:
        report = asyncio.run(_run(refresh_token, app_id, sign_key, args.token_url))
        payload: dict[str, object] = {
            "provider": "quark_open",
            "mode": "openlist_open_api",
            "persisted": False,
            "checks": asdict(report),
        }
        exit_code = 0
    except (ProviderError, ValueError) as exc:
        message = str(exc)
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
        payload = {
            "provider": "quark_open",
            "mode": "openlist_open_api",
            "persisted": False,
            "error": {"code": getattr(exc, "code", "QUARK_OPEN_PROBE_FAILED"), "message": message},
        }
        exit_code = 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
