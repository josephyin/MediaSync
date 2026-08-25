from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import subprocess
import sys
from dataclasses import asdict

from app.providers.quark.readonly_probe import (
    MAX_COOKIE_LENGTH,
    QuarkProbeError,
    QuarkReadOnlyProbe,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a non-persistent, read-only Quark Drive capability probe. "
            "The Cookie is read from the terminal and is never printed or saved."
        )
    )
    parser.add_argument(
        "--check-share",
        action="store_true",
        help="also prompt for a test share URL and inspect its first page",
    )
    cookie_source = parser.add_mutually_exclusive_group()
    cookie_source.add_argument(
        "--cookie-stdin",
        action="store_true",
        help=(
            "read the Cookie from standard input instead of the hidden terminal prompt; "
            "intended for a local clipboard pipe"
        ),
    )
    cookie_source.add_argument(
        "--cookie-clipboard",
        action="store_true",
        help="read the Cookie directly from the local macOS clipboard",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=10,
        help="maximum items requested per listing (1-50, default: 10)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="request timeout in seconds (default: 15)",
    )
    return parser


def _prompt_share_details(*, cookie_from_stdin: bool) -> tuple[str, str]:
    if not cookie_from_stdin:
        return (
            input("Quark share URL without password: ").strip(),
            getpass.getpass("Share password (hidden, optional): "),
        )

    try:
        with open("/dev/tty", "r+", encoding="utf-8") as terminal:
            terminal.write("Quark share URL without password: ")
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


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.cookie_stdin:
        cookie = sys.stdin.read(MAX_COOKIE_LENGTH + 1)
    elif args.cookie_clipboard:
        cookie = _read_cookie_from_clipboard()
    else:
        cookie = getpass.getpass("Quark Cookie (hidden, not saved): ")
    print("Cookie 已接收（仅在当前进程内使用，不保存）。", file=sys.stderr, flush=True)
    share_url: str | None = None
    share_password = ""
    if args.check_share:
        share_url, share_password = _prompt_share_details(
            cookie_from_stdin=args.cookie_stdin
        )

    total_stages = 3 if share_url is not None else 2
    stage_messages = {
        "account": f"[1/{total_stages}] 正在验证账号 Cookie…",
        "root": f"[2/{total_stages}] 正在读取根目录第一页…",
        "share": "[3/3] 正在读取测试分享第一页…",
        "complete": "只读检查完成，正在生成脱敏报告…",
    }

    def show_progress(stage: str) -> None:
        print(stage_messages[stage], file=sys.stderr, flush=True)

    async with QuarkReadOnlyProbe(cookie, timeout_seconds=args.timeout) as probe:
        report = await probe.run(
            share_url=share_url,
            share_password=share_password,
            page_size=args.page_size,
            progress=show_progress,
        )
    return {
        "schema_version": 1,
        "provider": "quark",
        "mode": "readonly_private_api",
        "persisted": False,
        "checks": asdict(report),
    }


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not 1 <= args.page_size <= 50:
        parser.error("--page-size must be between 1 and 50")
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout must be between 1 and 60 seconds")
    try:
        result = asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("诊断已取消；Cookie 未保存。", file=sys.stderr)
        raise SystemExit(130) from None
    except (QuarkProbeError, ValueError) as exc:
        result = {
            "schema_version": 1,
            "provider": "quark",
            "mode": "readonly_private_api",
            "persisted": False,
            "error": {
                "code": getattr(exc, "code", "QUARK_INPUT_INVALID"),
                "message": str(exc),
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
