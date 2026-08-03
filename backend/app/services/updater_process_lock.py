from __future__ import annotations

import errno
import fcntl
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType


class UpdaterProcessLockError(RuntimeError):
    """更新协调器独占锁不可用。"""


class UpdaterProcessLock:
    """持有更新协调器的进程级独占锁。"""

    def __init__(
        self,
        path: Path | str = Path("/data/update/updater.lock"),
        *,
        poll_interval_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("锁轮询间隔必须大于 0")
        self.path = Path(path)
        self.poll_interval_seconds = poll_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._file_descriptor: int | None = None

    @property
    def acquired(self) -> bool:
        return self._file_descriptor is not None

    def acquire(self, *, timeout_seconds: float = 0) -> None:
        if timeout_seconds < 0:
            raise ValueError("锁等待时间不能小于 0")
        if self.acquired:
            raise UpdaterProcessLockError("当前实例已经持有更新独占锁")

        file_descriptor = self._open_lock_file()
        deadline = self._monotonic() + timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(
                        file_descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    self._file_descriptor = file_descriptor
                    return
                except BlockingIOError as exc:
                    if self._monotonic() >= deadline:
                        raise UpdaterProcessLockError(
                            "更新协调器正在执行其他操作"
                        ) from exc
                    remaining = deadline - self._monotonic()
                    self._sleep(min(self.poll_interval_seconds, remaining))
        except BaseException:
            os.close(file_descriptor)
            raise

    def release(self) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return
        self._file_descriptor = None
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(file_descriptor)

    def __enter__(self) -> UpdaterProcessLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def _open_lock_file(self) -> int:
        self._prepare_directory()
        flags = os.O_RDWR | os.O_CREAT
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(
                self.path,
                flags | no_follow,
                0o600,
            )
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EISDIR}:
                raise UpdaterProcessLockError("更新锁文件路径不安全") from exc
            raise UpdaterProcessLockError("无法打开更新锁文件") from exc

        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise UpdaterProcessLockError("更新锁文件不是普通文件")
            os.fchmod(file_descriptor, 0o600)
        except BaseException:
            os.close(file_descriptor)
            raise
        return file_descriptor

    def _prepare_directory(self) -> None:
        directory = self.path.parent
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise UpdaterProcessLockError("无法创建更新锁目录") from exc
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise UpdaterProcessLockError("无法读取更新锁目录") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise UpdaterProcessLockError("更新锁目录路径不安全")
        try:
            directory.chmod(0o700)
        except OSError as exc:
            raise UpdaterProcessLockError("无法保护更新锁目录") from exc
