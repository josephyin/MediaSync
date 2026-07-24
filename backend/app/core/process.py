from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from types import FrameType

SignalCleanup = Callable[[], None]


def install_shutdown_signal_handlers(
    stop: asyncio.Event,
    *,
    logger: logging.Logger,
    process_name: str,
    loop: asyncio.AbstractEventLoop | None = None,
) -> SignalCleanup:
    if not process_name:
        raise ValueError("process_name must not be empty")

    event_loop = loop or asyncio.get_running_loop()
    installed_on_loop: list[signal.Signals] = []
    previous_handlers: dict[signal.Signals, signal.Handlers] = {}

    def request_stop(signal_name: str) -> None:
        logger.info(
            "process_stop_requested process=%s signal=%s",
            process_name,
            signal_name,
        )
        stop.set()

    for process_signal in (signal.SIGTERM, signal.SIGINT):
        try:
            event_loop.add_signal_handler(
                process_signal,
                request_stop,
                process_signal.name,
            )
            installed_on_loop.append(process_signal)
        except (NotImplementedError, RuntimeError):
            previous_handlers[process_signal] = signal.getsignal(process_signal)

            def fallback_handler(
                _signum: int,
                _frame: FrameType | None,
                *,
                signal_name: str = process_signal.name,
            ) -> None:
                request_stop(signal_name)

            signal.signal(process_signal, fallback_handler)

    def cleanup() -> None:
        for process_signal in installed_on_loop:
            event_loop.remove_signal_handler(process_signal)
        for process_signal, previous_handler in previous_handlers.items():
            signal.signal(process_signal, previous_handler)

    return cleanup
