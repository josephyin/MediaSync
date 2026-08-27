from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
from dataclasses import asdict

from app.providers.baidu.share_probe import (
    MAX_COOKIE_LENGTH,
    BaiduShareProbeError,
    BaiduShareReadOnlyProbe,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a non-persistent, read-only Baidu Web share probe. "
            "The Cookie is never printed or saved."
        )
    )
    cookie_source = parser.add_mutually_exclusive_group()
    cookie_source.add_argument(
        "--cookie-stdin",
        action="store_true",
        help="read the Cookie from standard input (for a local clipboard pipe)",
    )
    cookie_source.add_argument(
        "--cookie-clipboard",
        action="store_true",
        help="read the Cookie directly from the local macOS clipboard",
    )
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=15.0)
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


def _prompt_share_details(*, cookie_from_stdin: bool) -> tuple[str, str]:
    if not cookie_from_stdin:
        return (
            input("Baidu share URL: ").strip(),
            getpass.getpass("Share password (hidden, optional): "),
        )
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as terminal:
            terminal.write("Baidu share URL: ")
            terminal.flush()
            share_url = terminal.readline()
            if not share_url:
                raise ValueError("Interactive terminal closed before the share URL was read")
            share_password = getpass.getpass(
                "Share password (hidden, optional): ", stream=terminal
            )
    except OSError as exc:
        raise ValueError(
            "An interactive terminal is required when checking a share with --cookie-stdin"
        ) from exc
    return share_url.strip(), share_password


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.cookie_stdin:
        cookie = sys.stdin.read(MAX_COOKIE_LENGTH + 1)
    elif args.cookie_clipboard:
        cookie = _read_cookie_from_clipboard()
    else:
        cookie = getpass.getpass("Baidu Cookie (hidden, not saved): ")
    print("Cookie 已接收（仅在当前进程内使用，不保存）。", file=sys.stderr, flush=True)
    share_url, share_password = _prompt_share_details(cookie_from_stdin=args.cookie_stdin)
    messages = {
        "account": "[1/2] 正在验证百度 Web Cookie…",
        "share": "[2/2] 正在读取测试分享第一页…",
        "complete": "只读检查完成，正在生成脱敏报告…",
    }

    def show_progress(stage: str) -> None:
        print(messages[stage], file=sys.stderr, flush=True)

    async with BaiduShareReadOnlyProbe(cookie, timeout_seconds=args.timeout) as probe:
        report = await probe.run(
            share_url=share_url,
            password=share_password,
            page_size=args.page_size,
            progress=show_progress,
        )
    return {
        "schema_version": 1,
        "provider": "baidu",
        "mode": "readonly_web_share_api",
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
        print("诊断已取消；Cookie 未保存。", file=sys.stderr)
        raise SystemExit(130) from None
    except (BaiduShareProbeError, ValueError) as exc:
        result = {
            "schema_version": 1,
            "provider": "baidu",
            "mode": "readonly_web_share_api",
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

