from __future__ import annotations

import stat
from pathlib import Path

import pytest

from app.services.updater_process_lock import (
    UpdaterProcessLock,
    UpdaterProcessLockError,
)


def test_only_one_lock_owner_and_release_allows_next_owner(
    tmp_path: Path,
) -> None:
    path = tmp_path / "update" / "updater.lock"
    first = UpdaterProcessLock(path)
    second = UpdaterProcessLock(path)

    first.acquire()
    assert first.acquired is True
    with pytest.raises(UpdaterProcessLockError, match="正在执行其他操作"):
        second.acquire()

    first.release()
    second.acquire()
    assert second.acquired is True
    second.release()


def test_waiting_owner_acquires_after_current_owner_releases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "update" / "updater.lock"
    first = UpdaterProcessLock(path)
    first.acquire()
    clock = [0.0]

    def release_during_wait(seconds: float) -> None:
        clock[0] += seconds
        first.release()

    second = UpdaterProcessLock(
        path,
        poll_interval_seconds=0.1,
        monotonic=lambda: clock[0],
        sleep=release_during_wait,
    )
    second.acquire(timeout_seconds=1)

    assert second.acquired is True
    second.release()


def test_context_manager_releases_lock_after_failure(tmp_path: Path) -> None:
    path = tmp_path / "update" / "updater.lock"

    with pytest.raises(RuntimeError, match="operation failed"):
        with UpdaterProcessLock(path) as lock:
            assert lock.acquired is True
            raise RuntimeError("operation failed")

    with UpdaterProcessLock(path) as next_lock:
        assert next_lock.acquired is True


def test_lock_path_is_private_regular_file_and_is_not_deleted(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "update"
    path = directory / "updater.lock"
    lock = UpdaterProcessLock(path)

    lock.acquire()
    lock.release()

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_ISREG(path.lstat().st_mode)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_symlink_lock_path_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "update"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    path = directory / "updater.lock"
    path.symlink_to(target)

    with pytest.raises(UpdaterProcessLockError, match="路径不安全"):
        UpdaterProcessLock(path).acquire()

    assert target.read_text(encoding="utf-8") == "unchanged"


def test_non_directory_parent_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "update"
    directory.write_text("not a directory", encoding="utf-8")

    with pytest.raises(UpdaterProcessLockError, match="无法创建更新锁目录"):
        UpdaterProcessLock(directory / "updater.lock").acquire()


def test_invalid_wait_settings_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "update" / "updater.lock"

    with pytest.raises(ValueError, match="轮询间隔"):
        UpdaterProcessLock(path, poll_interval_seconds=0)
    with pytest.raises(ValueError, match="等待时间"):
        UpdaterProcessLock(path).acquire(timeout_seconds=-1)
