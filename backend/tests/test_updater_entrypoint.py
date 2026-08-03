from __future__ import annotations

import pytest

from app.updater import __main__ as updater_main


class FakeCoordinator:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def run_once(self) -> str:
        self.calls += 1
        return next(self.outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [("completed", 0), ("manual_recovery", 2)],
)
async def test_updater_entrypoint_has_fixed_terminal_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    exit_code: int,
) -> None:
    coordinator = FakeCoordinator([outcome])
    monkeypatch.setattr(updater_main, "build_coordinator", lambda: coordinator)

    assert await updater_main.run() == exit_code
    assert coordinator.calls == 1


@pytest.mark.asyncio
async def test_updater_entrypoint_waits_instead_of_exiting_before_disarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = FakeCoordinator(["waiting_for_disarm", "completed"])
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(updater_main, "build_coordinator", lambda: coordinator)
    monkeypatch.setattr(updater_main.asyncio, "sleep", fake_sleep)

    assert await updater_main.run() == 0
    assert coordinator.calls == 2
    assert waits == [5]
