import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class AliyunDriveRequestGuard:
    """Serialize private API calls and provide bounded retry delays."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_allowed_at = 0.0

    @asynccontextmanager
    async def slot(self, interval_seconds: float, jitter_seconds: float) -> AsyncIterator[None]:
        async with self._lock:
            delay = self._next_allowed_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                yield
            finally:
                jitter = random.uniform(0, max(jitter_seconds, 0))
                self._next_allowed_at = time.monotonic() + max(interval_seconds, 0) + jitter

    @staticmethod
    async def backoff(
        attempt: int,
        base_seconds: float,
        max_seconds: float,
        retry_after: float | None = None,
    ) -> None:
        if retry_after is not None:
            delay = min(max(retry_after, 0), max_seconds)
        else:
            delay = min(base_seconds * (2**attempt), max_seconds)
            delay += random.uniform(0, min(max(base_seconds, 0), 1.0))
        if delay > 0:
            await asyncio.sleep(delay)


request_guard = AliyunDriveRequestGuard()
