from __future__ import annotations

from typing import Literal

from app.core.config import Settings

BackgroundExecutionMode = Literal["legacy", "process"]


class BackgroundExecutionModeError(RuntimeError):
    def __init__(
        self,
        *,
        process_name: str,
        expected_mode: BackgroundExecutionMode,
        actual_mode: BackgroundExecutionMode,
    ) -> None:
        self.process_name = process_name
        self.expected_mode = expected_mode
        self.actual_mode = actual_mode
        super().__init__(
            f"{process_name} requires background execution mode "
            f"{expected_mode!r}, found {actual_mode!r}"
        )


def require_background_execution_mode(
    settings: Settings,
    *,
    process_name: str,
    expected_mode: BackgroundExecutionMode,
) -> None:
    if not process_name:
        raise ValueError("process_name must not be empty")
    if settings.background_execution_mode != expected_mode:
        raise BackgroundExecutionModeError(
            process_name=process_name,
            expected_mode=expected_mode,
            actual_mode=settings.background_execution_mode,
        )
