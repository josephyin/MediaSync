from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
from dataclasses import asdict

from app.providers.baidu.readonly_probe import (
    MAX_ACCESS_TOKEN_LENGTH,
    BaiduOpenReadOnlyProbe,
    BaiduProbeError,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a non-persistent, read-only Baidu Netdisk OpenAPI probe. "
            "The Access Token is never printed or saved."
        )
    )
    token_source = parser.add_mutually_exclusive_group()
    token_source.add_argument(
        "--token-stdin",
        action="store_true",
        help="read the Access Token from standard input (for a local clipboard pipe)",
    )
    token_source.add_argument(
        "--token-clipboard",
        action="store_true",
        help="read the Access Token directly from the local macOS clipboard",
    )
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser


def _read_token_from_clipboard() -> str:
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


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.token_stdin:
        token = sys.stdin.read(MAX_ACCESS_TOKEN_LENGTH + 1)
    elif args.token_clipboard:
        token = _read_token_from_clipboard()
    else:
        token = getpass.getpass("Baidu Access Token (hidden, not saved): ")
    print("Access Token 已接收（仅在当前进程内使用，不保存）。", file=sys.stderr, flush=True)
    messages = {
        "account": "[1/2] 正在验证百度网盘 OpenAPI 账号…",
        "root": "[2/2] 正在读取根目录第一页…",
        "complete": "只读检查完成，正在生成脱敏报告…",
    }

    def show_progress(stage: str) -> None:
        print(messages[stage], file=sys.stderr, flush=True)

    async with BaiduOpenReadOnlyProbe(token, timeout_seconds=args.timeout) as probe:
        report = await probe.run(page_size=args.page_size, progress=show_progress)
    return {
        "schema_version": 1,
        "provider": "baidu",
        "mode": "official_open_api",
        "persisted": False,
        "checks": asdict(report),
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100:
        parser.error("--page-size must be between 1 and 100")
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout must be between 1 and 60 seconds")
    try:
        result = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("诊断已取消；Access Token 未保存。", file=sys.stderr)
        raise SystemExit(130) from None
    except (BaiduProbeError, ValueError) as exc:
        result = {
            "schema_version": 1,
            "provider": "baidu",
            "mode": "official_open_api",
            "persisted": False,
            "error": {
                "code": getattr(exc, "code", "BAIDU_INPUT_INVALID"),
                "message": str(exc),
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

