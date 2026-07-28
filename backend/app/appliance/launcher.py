from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol

from app.core.runtime_secrets import RuntimeSecretsError, prepare_runtime_secrets

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIRECTORY = Path("/data")
DEFAULT_DATABASE_URL = "sqlite:////data/mediasync.db"
DEFAULT_API_HEALTH_URL = "http://127.0.0.1:8000/api/v1/system/health"


class LauncherError(RuntimeError):
    """Appliance 无法安全完成启动或监管时抛出。"""


class ManagedProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    name: str
    command: tuple[str, ...]
    shutdown_timeout_seconds: float


MIGRATION = ProcessSpec(
    name="migration",
    command=("alembic", "upgrade", "head"),
    shutdown_timeout_seconds=0,
)
RECONCILIATION = ProcessSpec(
    name="reconciliation",
    command=(sys.executable, "-m", "app.reconcile"),
    shutdown_timeout_seconds=0,
)
API = ProcessSpec(
    name="api",
    command=(
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        "1",
    ),
    shutdown_timeout_seconds=30,
)
NGINX = ProcessSpec(
    name="nginx",
    command=("nginx", "-g", "daemon off;"),
    shutdown_timeout_seconds=10,
)
SCHEDULER = ProcessSpec(
    name="scheduler",
    command=(sys.executable, "-m", "app.scheduler"),
    shutdown_timeout_seconds=30,
)
WORKER = ProcessSpec(
    name="worker",
    command=(sys.executable, "-m", "app.worker"),
    shutdown_timeout_seconds=90,
)

BARRIER_SPECS = (MIGRATION, RECONCILIATION)
STARTUP_SPECS = (NGINX, SCHEDULER, WORKER)
SHUTDOWN_ORDER = ("nginx", "scheduler", "worker", "api")

BarrierRunner = Callable[[ProcessSpec, Mapping[str, str]], int]
ProcessFactory = Callable[[ProcessSpec, Mapping[str, str]], ManagedProcess]
ApiWaiter = Callable[[ManagedProcess, float], None]
Sleep = Callable[[float], None]


def _run_barrier(spec: ProcessSpec, environment: Mapping[str, str]) -> int:
    completed = subprocess.run(  # noqa: S603
        list(spec.command),
        env=dict(environment),
        check=False,
    )
    return completed.returncode


def _start_process(
    spec: ProcessSpec,
    environment: Mapping[str, str],
) -> ManagedProcess:
    return subprocess.Popen(  # noqa: S603
        list(spec.command),
        env=dict(environment),
    )


def _wait_for_api(
    process: ManagedProcess,
    timeout_seconds: float,
    *,
    url: str = DEFAULT_API_HEALTH_URL,
    poll_interval_seconds: float = 0.25,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise LauncherError(
                f"API process exited before becoming healthy (exit code {return_code})"
            )

        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310
                if 200 <= response.status < 300:
                    return
                last_error = LauncherError(
                    f"API health endpoint returned HTTP {response.status}"
                )
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            last_error = exc

        time.sleep(poll_interval_seconds)

    detail = f": {last_error}" if last_error is not None else ""
    raise LauncherError(
        f"API did not become healthy within {timeout_seconds:g} seconds{detail}"
    )


class ApplianceLauncher:
    """管理单容器 Appliance 的启动屏障和常驻子进程。"""

    def __init__(
        self,
        *,
        data_directory: Path = DEFAULT_DATA_DIRECTORY,
        environment: Mapping[str, str] | None = None,
        barrier_runner: BarrierRunner = _run_barrier,
        process_factory: ProcessFactory = _start_process,
        api_waiter: ApiWaiter = _wait_for_api,
        api_startup_timeout_seconds: float = 30,
        supervision_poll_seconds: float = 0.5,
        sleep: Sleep = time.sleep,
    ) -> None:
        if api_startup_timeout_seconds <= 0:
            raise ValueError("api_startup_timeout_seconds must be positive")
        if supervision_poll_seconds <= 0:
            raise ValueError("supervision_poll_seconds must be positive")

        self._data_directory = Path(data_directory)
        self._base_environment = dict(os.environ if environment is None else environment)
        self._barrier_runner = barrier_runner
        self._process_factory = process_factory
        self._api_waiter = api_waiter
        self._api_startup_timeout_seconds = api_startup_timeout_seconds
        self._supervision_poll_seconds = supervision_poll_seconds
        self._sleep = sleep
        self._children: dict[str, tuple[ProcessSpec, ManagedProcess]] = {}

    def prepare_environment(self) -> dict[str, str]:
        preparation = prepare_runtime_secrets(
            self._data_directory,
            environment=self._base_environment,
        )
        if preparation.initial_admin_password is not None:
            logger.warning(
                "appliance_initial_admin_password username=%s password=%s "
                "message=请立即登录并妥善保存；该密码后续启动不会再次显示",
                self._base_environment.get("ADMIN_USERNAME", "admin"),
                preparation.initial_admin_password,
            )

        environment = dict(self._base_environment)
        environment.update(preparation.values.as_environment())
        environment.update(
            {
                "DATABASE_URL": DEFAULT_DATABASE_URL,
                "BACKGROUND_EXECUTION_MODE": "process",
                "ENVIRONMENT": "production",
            }
        )
        return environment

    def run(
        self,
        *,
        stop: threading.Event | None = None,
        install_signal_handlers: bool = True,
    ) -> int:
        stop_event = stop or threading.Event()

        def cleanup_signals() -> None:
            pass

        exit_code = 0

        if install_signal_handlers:
            cleanup_signals = self._install_signal_handlers(stop_event)

        try:
            environment = self.prepare_environment()
            self._run_startup_barriers(environment)
            self._start(API, environment)
            self._api_waiter(
                self._children["api"][1],
                self._api_startup_timeout_seconds,
            )
            for spec in STARTUP_SPECS:
                self._start(spec, environment)

            logger.info(
                "appliance_started child_processes=%s",
                ",".join(self._children),
            )
            exit_code = self._supervise(stop_event)
        except (LauncherError, RuntimeSecretsError):
            logger.exception("appliance_start_failed")
            exit_code = 1
        except Exception:
            logger.exception("appliance_unexpected_failure")
            exit_code = 1
        finally:
            cleanup_signals()
            self._shutdown_children()

        logger.info("appliance_stopped exit_code=%d", exit_code)
        return exit_code

    def _run_startup_barriers(self, environment: Mapping[str, str]) -> None:
        for spec in BARRIER_SPECS:
            logger.info(
                "appliance_barrier_started name=%s command=%s",
                spec.name,
                list(spec.command),
            )
            return_code = self._barrier_runner(spec, environment)
            if return_code != 0:
                raise LauncherError(
                    f"Startup barrier {spec.name} failed with exit code {return_code}"
                )
            logger.info("appliance_barrier_completed name=%s", spec.name)

    def _start(self, spec: ProcessSpec, environment: Mapping[str, str]) -> None:
        if spec.name in self._children:
            raise LauncherError(f"Process {spec.name} was already started")
        process = self._process_factory(spec, environment)
        self._children[spec.name] = (spec, process)
        logger.info(
            "appliance_child_started name=%s pid=%d",
            spec.name,
            process.pid,
        )

    def _supervise(self, stop: threading.Event) -> int:
        while not stop.is_set():
            for name, (_spec, process) in self._children.items():
                return_code = process.poll()
                if return_code is not None:
                    logger.error(
                        "appliance_child_exited name=%s pid=%d exit_code=%d",
                        name,
                        process.pid,
                        return_code,
                    )
                    return return_code if return_code != 0 else 1
            self._sleep(self._supervision_poll_seconds)
        return 0

    def _shutdown_children(self) -> None:
        if not self._children:
            return

        for name in SHUTDOWN_ORDER:
            child = self._children.get(name)
            if child is None:
                continue
            spec, process = child
            if process.poll() is not None:
                continue
            logger.info(
                "appliance_child_stop_requested name=%s pid=%d",
                name,
                process.pid,
            )
            process.terminate()
            try:
                process.wait(timeout=spec.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                logger.error(
                    "appliance_child_force_killed name=%s pid=%d",
                    name,
                    process.pid,
                )
                process.kill()
                process.wait()

        self._children.clear()

    @staticmethod
    def _install_signal_handlers(
        stop: threading.Event,
    ) -> Callable[[], None]:
        previous_handlers: dict[signal.Signals, signal.Handlers] = {}

        def request_stop(signum: int, _frame: FrameType | None) -> None:
            process_signal = signal.Signals(signum)
            logger.info(
                "appliance_stop_requested signal=%s",
                process_signal.name,
            )
            stop.set()

        for process_signal in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[process_signal] = signal.getsignal(process_signal)
            signal.signal(process_signal, request_stop)

        def cleanup() -> None:
            for process_signal, previous_handler in previous_handlers.items():
                signal.signal(process_signal, previous_handler)

        return cleanup


def main() -> None:
    logging.basicConfig(
        level=getattr(
            logging,
            os.environ.get("LOG_LEVEL", "INFO").upper(),
            logging.INFO,
        ),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    raise SystemExit(ApplianceLauncher().run())


if __name__ == "__main__":
    main()
