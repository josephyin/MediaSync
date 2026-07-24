import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main as main_module
from app import worker as worker_module
from app.core.config import Settings
from app.core.execution import (
    BackgroundExecutionModeError,
    require_background_execution_mode,
)
from app.scheduler import runtime as scheduler_module


def settings(mode: str = "legacy") -> Settings:
    return Settings(
        _env_file=None,
        background_execution_mode=mode,
    )


def test_background_execution_mode_defaults_to_legacy() -> None:
    assert Settings(_env_file=None).background_execution_mode == "legacy"


def test_background_execution_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        settings("distributed")


def test_mode_guard_reports_expected_and_actual_modes() -> None:
    with pytest.raises(BackgroundExecutionModeError) as raised:
        require_background_execution_mode(
            settings("legacy"),
            process_name="worker",
            expected_mode="process",
        )

    assert raised.value.process_name == "worker"
    assert raised.value.expected_mode == "process"
    assert raised.value.actual_mode == "legacy"


async def test_worker_refuses_legacy_mode_before_runtime_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_build(**_kwargs: object) -> object:
        raise AssertionError("worker runtime must not be built in legacy mode")

    monkeypatch.setattr(worker_module, "build_worker_runtime", unexpected_build)

    with pytest.raises(BackgroundExecutionModeError):
        await worker_module.run_worker(
            settings=settings("legacy"),
            install_signal_handlers=False,
        )


async def test_scheduler_refuses_legacy_mode_before_runtime_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_build(**_kwargs: object) -> object:
        raise AssertionError("scheduler runtime must not be built in legacy mode")

    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler_runtime",
        unexpected_build,
    )

    with pytest.raises(BackgroundExecutionModeError):
        await scheduler_module.run_scheduler(
            settings=settings("legacy"),
            install_signal_handlers=False,
        )


@pytest.mark.parametrize(
    "process_module",
    [worker_module, scheduler_module],
    ids=["worker", "scheduler"],
)
def test_process_main_exits_with_configuration_error_in_legacy_mode(
    process_module: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        process_module,
        "get_settings",
        lambda: settings("legacy"),
    )

    with pytest.raises(SystemExit) as raised:
        process_module.main()

    assert raised.value.code == 2


async def test_api_legacy_startup_reports_selected_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info_events: list[tuple[object, ...]] = []
    monkeypatch.setattr(main_module.settings, "background_execution_mode", "legacy")
    monkeypatch.setattr(
        main_module.logger,
        "info",
        lambda *args: info_events.append(args),
    )
    monkeypatch.setattr(
        main_module.Base.metadata,
        "create_all",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(main_module, "start_scheduler", lambda: None)
    monkeypatch.setattr(main_module, "stop_scheduler", lambda: None)

    async with main_module.lifespan(main_module.app):
        pass

    assert (
        "background_execution_mode_selected process=api mode=%s",
        "legacy",
    ) in info_events


async def test_api_refuses_process_mode_before_database_or_scheduler_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main_module.settings, "background_execution_mode", "process")

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("API runtime side effect must not run in process mode yet")

    monkeypatch.setattr(main_module.Base.metadata, "create_all", unexpected_call)
    monkeypatch.setattr(main_module, "start_scheduler", unexpected_call)

    with pytest.raises(BackgroundExecutionModeError):
        async with main_module.lifespan(main_module.app):
            pass


def test_existing_api_still_starts_in_default_legacy_mode() -> None:
    with TestClient(main_module.app) as client:
        response = client.get("/api/v1/system/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
