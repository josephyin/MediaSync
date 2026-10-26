"""Range downloads with private checkpoints, GCID verification and no overwrite."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx


class DownloadError(ValueError):
    pass


class DownloadDeferred(Exception):
    def __init__(self, count):
        self.count = count


def validate_download_url(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not (host == "mypikpak.com" or host.endswith(".mypikpak.com"))
    ):
        raise DownloadError("PikPak 返回了不受支持的下载地址")


def _checkpoint(path, state):
    # The partial-file lock serializes checkpoint writers. Never follow links.
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(state, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def _file_identity(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _read_checkpoint(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            value = json.load(source)
            return value if isinstance(value, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


async def _verify(output, state, expected_size, expected_hash, probe, chunk_bytes, deadline):
    if expected_hash is None:
        return
    if not re.fullmatch(r"[0-9a-fA-F]{40}", expected_hash):
        raise DownloadError("PikPak 文件 hash 格式无法识别，未发布文件")
    # PikPak's hash is GCID: SHA-1 over the binary SHA-1 digests of file blocks.
    block = 256 * 1024
    while expected_size > block * 512 and block < 2 * 1024 * 1024:
        block *= 2
    digests = state.setdefault("digests", [])
    position = min(len(digests) * block, expected_size)
    output.seek(position)
    checked = 0
    while position < expected_size:
        await probe()
        content = await asyncio.to_thread(output.read, min(block, expected_size - position))
        if not content:
            raise DownloadError("下载文件在校验期间发生变化")
        digests.append(hashlib.sha1(content).hexdigest())
        checked += len(content)
        position += len(content)
        if position < expected_size and (
            chunk_bytes and (checked >= chunk_bytes or time.monotonic() >= deadline)
        ):
            raise DownloadDeferred(expected_size)
    actual = hashlib.sha1(b"".join(bytes.fromhex(x) for x in digests)).hexdigest()
    if actual.lower() != expected_hash.lower():
        output.seek(0)
        output.truncate()
        state["digests"] = []
        raise DownloadError("PikPak GCID 内容校验失败，未发布文件；重试将重新下载")


async def stream_download(
    url,
    target,
    expected_size,
    progress,
    cancelled,
    *,
    client=None,
    identity=None,
    expected_hash=None,
    chunk_bytes=None,
    rate_limit=0,
    guard=None,
):
    """A bounded call saves its partial and raises DownloadDeferred to yield the worker."""
    validate_download_url(url)
    if expected_size is None or expected_size < 0:
        raise DownloadError("文件大小未知，无法安全校验下载")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name("." + target.name + ".mediasync.part")
    checkpoint = target.with_name("." + target.name + ".mediasync.json")
    fingerprint = hashlib.sha256(
        json.dumps([identity, expected_size, expected_hash], separators=(",", ":")).encode()
    ).hexdigest()
    fd = os.open(partial, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), trust_env=False)
    deadline = time.monotonic() + 8

    async def probe():
        if await cancelled():
            raise asyncio.CancelledError
        if guard is not None:
            guard()

    try:
        with os.fdopen(fd, "r+b") as output:
            try:
                fcntl.flock(output, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DownloadError("同一目标文件已有下载任务") from None
            await probe()
            state = _read_checkpoint(checkpoint)
            if target.exists() or target.is_symlink():
                if (
                    not target.is_symlink()
                    and state.get("identity") == fingerprint
                    and state.get("published_identity") == _file_identity(target)
                ):
                    # Also covers a crash after link/unlink but before DB finalization.
                    partial.unlink()
                    return expected_size
                raise DownloadError("目标文件已存在，请换一个目录；不会覆盖现有文件")
            if (
                state.get("identity") != fingerprint
                or os.fstat(output.fileno()).st_size > expected_size
            ):
                if (
                    state.get("identity")
                    and state.get("identity") != fingerprint
                    and os.fstat(output.fileno()).st_size > 0
                ):
                    raise DownloadError("此路径存在其他文件的下载进度，请选择不同子目录")
                output.truncate(0)
                state = {"identity": fingerprint, "digests": []}
            _checkpoint(checkpoint, state)
            try:
                start = output.seek(0, os.SEEK_END)
                written = start
                if start < expected_size:
                    end = (
                        min(expected_size, start + chunk_bytes) - 1
                        if chunk_bytes
                        else expected_size - 1
                    )
                    headers = {"Accept-Encoding": "identity"}
                    if start or chunk_bytes:
                        headers["Range"] = f"bytes={start}-{end}"
                    for redirects in range(6):
                        validate_download_url(url)
                        async with client.stream(
                            "GET", url, headers=headers, follow_redirects=False
                        ) as response:
                            if response.is_redirect:
                                if redirects == 5 or "location" not in response.headers:
                                    raise DownloadError("下载重定向次数过多")
                                url = str(response.url.join(response.headers["location"]))
                                continue
                            if response.headers.get("content-encoding", "identity") != "identity":
                                raise DownloadError("下载接口返回了不支持的压缩编码")
                            if response.status_code == 206:
                                match = re.fullmatch(
                                    r"bytes (\d+)-(\d+)/(\d+)",
                                    response.headers.get("content-range", ""),
                                )
                                if not match or not (
                                    int(match[1]) == start
                                    and start <= int(match[2]) <= end
                                    and int(match[3]) == expected_size
                                ):
                                    raise DownloadError("下载断点响应范围不匹配，未追加内容")
                                response_end = int(match[2]) + 1
                            elif response.status_code == 200:
                                if start:
                                    # Ignored Range must replace, never append to the partial.
                                    output.seek(0)
                                    output.truncate()
                                    written = 0
                                    state["digests"] = []
                                if chunk_bytes and expected_size > chunk_bytes:
                                    raise DownloadError(
                                        "下载接口不支持 Range，无法安全分段下载此大文件"
                                    )
                                response_end = expected_size
                            else:
                                raise DownloadError(
                                    f"下载请求失败（HTTP {response.status_code}），可重试获取新链接"
                                )
                            received = 0
                            started = time.monotonic()
                            read_size = (
                                min(65536, max(1024, rate_limit // 4)) if rate_limit else 65536
                            )
                            async for content in response.aiter_bytes(read_size):
                                await probe()
                                if written + len(content) > response_end:
                                    raise DownloadError("下载内容超出声明的文件范围")
                                await asyncio.to_thread(output.write, content)
                                written += len(content)
                                received += len(content)
                                await progress(written)
                                if rate_limit:
                                    delay = received / rate_limit - (time.monotonic() - started)
                                    while delay > 0:
                                        await probe()
                                        await asyncio.sleep(min(delay, 0.25))
                                        delay = received / rate_limit - (time.monotonic() - started)
                                if (
                                    chunk_bytes
                                    and written < expected_size
                                    and time.monotonic() >= deadline
                                ):
                                    raise DownloadDeferred(written)
                            if written != response_end:
                                raise DownloadError("文件大小校验失败，未发布不完整文件")
                            break
                if written < expected_size:
                    raise DownloadDeferred(written)
                output.flush()
                await _verify(
                    output, state, expected_size, expected_hash, probe, chunk_bytes, deadline
                )
                await probe()
                output.flush()
                await asyncio.to_thread(os.fsync, output.fileno())
                state["published_identity"] = _file_identity(partial)
                state.pop("digests", None)
                _checkpoint(checkpoint, state)
                await probe()
                # No await between the last ownership check and atomic publication.
                os.link(partial, target)
                partial.unlink()
                await progress(written)
                return written
            finally:
                output.flush()
                os.fsync(output.fileno())
                if partial.exists():
                    _checkpoint(checkpoint, state)
    finally:
        if owned:
            await client.aclose()
