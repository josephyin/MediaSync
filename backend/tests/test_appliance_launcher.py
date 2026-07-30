from __future__ import annotations

import json
import logging
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from app.appliance.launcher import (
    API,
    DEFAULT_DATABASE_URL,
    NGINX,
    RECONCILIATION,
    SCHEDULER,
    WORKER,
    ApplianceLauncher,
    ProcessSpec,
)

SECRET_KEY = "secret-key-for-appliance-tests"
CREDENTIAL_KEY = "credential-key-for-appliance-tests"
ADMIN_PASSWORD = "admin-password-for-appliance-tests"


class FakeProcess:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        pid: int,
        exit_code: int | None = None,
        timeout_on_wait: bool = False,
    ) -> None:
        self.name = name
        self.events = events
        self.pid = pid
        self.exit_code = exit_code
        self.timeout_on_wait = timeout_on_wait
        self.terminate_requested = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")
        self.terminate_requested = True

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        self.events.append(f"wait:{self.name}:{timeout}")
        if self.timeout_on_wait and self.exit_code is None:
            self.timeout_on_wait = False
            raise subprocess.TimeoutExpired(self.name, timeout)
        if self.terminate_requested and self.exit_code is None:
            self.exit_code = 0
        return self.exit_code or 0


def appliance_environment() -> dict[str, str]:
    return {
        "SECRET_KEY": SECRET_KEY,
        "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
    }


def short_socket_path() -> Path:
    return Path.cwd() / f".launcher-{uuid4().hex[:12]}.sock"


def test_prepare_environment_persists_required_runtime_values(
    tmp_path: Path,
) -> None:
    launcher = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
    )

    environment = launcher.prepare_environment()

    assert environment["SECRET_KEY"] == SECRET_KEY
    assert environment["CREDENTIAL_ENCRYPTION_KEY"] == CREDENTIAL_KEY
    assert environment["ADMIN_PASSWORD"] == ADMIN_PASSWORD
    assert environment["ADMIN_SESSION_REVISION"] == "0"
    assert environment["ADMIN_PASSWORD_CHANGE_SUPPORTED"] == "true"
    assert environment["RUNTIME_SECRETS_PATH"] == str(
        tmp_path / "config" / "runtime-secrets.json"
    )
    assert environment["DATABASE_URL"] == DEFAULT_DATABASE_URL
    assert environment["BACKGROUND_EXECUTION_MODE"] == "process"
    assert environment["ENVIRONMENT"] == "production"


def test_offline_password_reset_increments_session_revision(
    tmp_path: Path,
) -> None:
    first = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
    ).prepare_environment()
    reset_environment = appliance_environment()
    reset_environment["ADMIN_PASSWORD"] = "reset-admin-password"

    reset = ApplianceLauncher(
        data_directory=tmp_path,
        environment=reset_environment,
    ).prepare_environment()

    assert first["ADMIN_SESSION_REVISION"] == "0"
    assert reset["ADMIN_PASSWORD"] == "reset-admin-password"
    assert reset["ADMIN_SESSION_REVISION"] == "1"


def test_generated_admin_password_is_logged_only_on_first_start(
    tmp_path: Path,
    caplog,
) -> None:
    environment = {
        "SECRET_KEY": SECRET_KEY,
        "CREDENTIAL_ENCRYPTION_KEY": CREDENTIAL_KEY,
    }
    caplog.set_level(logging.WARNING)

    first_environment = ApplianceLauncher(
        data_directory=tmp_path,
        environment=environment,
    ).prepare_environment()
    first_messages = [record.getMessage() for record in caplog.records]

    assert first_environment["ADMIN_PASSWORD"] in first_messages[0]
    assert SECRET_KEY not in caplog.text
    assert CREDENTIAL_KEY not in caplog.text

    caplog.clear()
    second_environment = ApplianceLauncher(
        data_directory=tmp_path,
        environment=environment,
    ).prepare_environment()

    assert second_environment["ADMIN_PASSWORD"] == first_environment["ADMIN_PASSWORD"]
    assert caplog.records == []


def test_explicit_admin_password_is_never_logged(
    tmp_path: Path,
    caplog,
) -> None:
    caplog.set_level(logging.WARNING)

    ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
    ).prepare_environment()

    assert ADMIN_PASSWORD not in caplog.text


def test_launcher_runs_barriers_then_starts_processes_in_contract_order(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    stop = threading.Event()

    def barrier_runner(spec: ProcessSpec, environment: Mapping[str, str]) -> int:
        assert environment["BACKGROUND_EXECUTION_MODE"] == "process"
        events.append(f"barrier:{spec.name}")
        return 0

    def process_factory(
        spec: ProcessSpec,
        environment: Mapping[str, str],
    ) -> FakeProcess:
        assert environment["DATABASE_URL"] == DEFAULT_DATABASE_URL
        events.append(f"start:{spec.name}")
        return FakeProcess(spec.name, events, pid=100 + len(events))

    def api_waiter(process: FakeProcess, timeout: float) -> None:
        assert process.name == API.name
        assert timeout == 12
        events.append("healthy:api")

    def sleep(_seconds: float) -> None:
        stop.set()

    launcher = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
        barrier_runner=barrier_runner,
        process_factory=process_factory,
        api_waiter=api_waiter,
        api_startup_timeout_seconds=12,
        sleep=sleep,
        health_socket_path=short_socket_path(),
    )

    exit_code = launcher.run(stop=stop, install_signal_handlers=False)

    assert exit_code == 0
    assert events[:8] == [
        "barrier:migration",
        "barrier:reconciliation",
        "start:api",
        "healthy:api",
        "start:nginx",
        "start:scheduler",
        "start:worker",
        "terminate:nginx",
    ]
    assert [event for event in events if event.startswith("terminate:")] == [
        "terminate:nginx",
        "terminate:scheduler",
        "terminate:worker",
        "terminate:api",
    ]


def test_barrier_failure_prevents_all_process_startup(tmp_path: Path) -> None:
    events: list[str] = []

    def barrier_runner(spec: ProcessSpec, _environment: Mapping[str, str]) -> int:
        events.append(f"barrier:{spec.name}")
        return 7 if spec.name == RECONCILIATION.name else 0

    def process_factory(
        spec: ProcessSpec,
        _environment: Mapping[str, str],
    ) -> FakeProcess:
        events.append(f"start:{spec.name}")
        return FakeProcess(spec.name, events, pid=123)

    launcher = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
        barrier_runner=barrier_runner,
        process_factory=process_factory,
    )

    exit_code = launcher.run(install_signal_handlers=False)

    assert exit_code == 1
    assert events == ["barrier:migration", "barrier:reconciliation"]


def test_api_health_failure_stops_api_and_prevents_other_processes(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def process_factory(
        spec: ProcessSpec,
        _environment: Mapping[str, str],
    ) -> FakeProcess:
        events.append(f"start:{spec.name}")
        return FakeProcess(spec.name, events, pid=123)

    def api_waiter(_process: FakeProcess, _timeout: float) -> None:
        raise RuntimeError("API unavailable")

    launcher = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
        barrier_runner=lambda _spec, _environment: 0,
        process_factory=process_factory,
        api_waiter=api_waiter,
    )

    exit_code = launcher.run(install_signal_handlers=False)

    assert exit_code == 1
    assert events == ["start:api", "terminate:api", "wait:api:30"]


def test_critical_child_exit_stops_remaining_processes_and_fails(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    def process_factory(
        spec: ProcessSpec,
        _environment: Mapping[str, str],
    ) -> FakeProcess:
        exit_code = -9 if spec.name == SCHEDULER.name else None
        return FakeProcess(spec.name, events, pid=123, exit_code=exit_code)

    launcher = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
        barrier_runner=lambda _spec, _environment: 0,
        process_factory=process_factory,
        api_waiter=lambda _process, _timeout: None,
        sleep=lambda _seconds: None,
        health_socket_path=short_socket_path(),
    )

    exit_code = launcher.run(install_signal_handlers=False)

    assert exit_code == 137
    assert [event for event in events if event.startswith("terminate:")] == [
        "terminate:nginx",
        "terminate:worker",
        "terminate:api",
    ]


def test_shutdown_force_kills_process_after_its_grace_period(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    stop = threading.Event()

    def process_factory(
        spec: ProcessSpec,
        _environment: Mapping[str, str],
    ) -> FakeProcess:
        return FakeProcess(
            spec.name,
            events,
            pid=123,
            timeout_on_wait=spec.name == WORKER.name,
        )

    def sleep(_seconds: float) -> None:
        stop.set()

    launcher = ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
        barrier_runner=lambda _spec, _environment: 0,
        process_factory=process_factory,
        api_waiter=lambda _process, _timeout: None,
        sleep=sleep,
        health_socket_path=short_socket_path(),
    )

    exit_code = launcher.run(stop=stop, install_signal_handlers=False)

    assert exit_code == 0
    assert f"wait:{WORKER.name}:{WORKER.shutdown_timeout_seconds}" in events
    worker_wait_index = events.index(f"wait:{WORKER.name}:{WORKER.shutdown_timeout_seconds}")
    assert events[worker_wait_index + 1 : worker_wait_index + 3] == [
        "kill:worker",
        "wait:worker:None",
    ]


def test_child_commands_are_explicit_argument_lists() -> None:
    assert API.command == (
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        "1",
    )
    assert NGINX.command == (
        "nginx",
        "-g",
        "daemon off;",
        "-c",
        "/etc/nginx/nginx-appliance.conf",
    )
    assert SCHEDULER.command[-2:] == ("-m", "app.scheduler")
    assert WORKER.command[-2:] == ("-m", "app.worker")


def test_runtime_secrets_file_keeps_versioned_json_contract(tmp_path: Path) -> None:
    ApplianceLauncher(
        data_directory=tmp_path,
        environment=appliance_environment(),
    ).prepare_environment()

    payload = json.loads(
        (tmp_path / "config" / "runtime-secrets.json").read_text(encoding="utf-8")
    )

    assert payload["version"] == 1
