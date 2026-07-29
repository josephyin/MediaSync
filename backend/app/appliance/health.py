from __future__ import annotations

import json
import logging
import socket
import stat
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HEALTH_SOCKET_PATH = Path("/run/mediasync/health.sock")
DEFAULT_NGINX_HEALTH_URL = "http://127.0.0.1:9090/api/v1/system/health"
REQUIRED_COMPONENTS = ("launcher", "nginx", "api", "scheduler", "worker")
MAX_HEALTH_RESPONSE_BYTES = 16 * 1024

StatusProvider = Callable[[], Mapping[str, bool]]
HttpProbe = Callable[[str, float], bool]


class HealthCheckError(RuntimeError):
    """无法取得可信的 Appliance 健康状态。"""


class ApplianceHealthServer:
    """通过仅限容器本地的 Unix Socket 暴露 Launcher 子进程状态。"""

    def __init__(
        self,
        *,
        socket_path: Path = DEFAULT_HEALTH_SOCKET_PATH,
        status_provider: StatusProvider,
    ) -> None:
        self._socket_path = Path(socket_path)
        self._status_provider = status_provider
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._server_socket: socket.socket | None = None
        self._startup_error: Exception | None = None

    def start(self, *, timeout_seconds: float = 5) -> None:
        if self._thread is not None:
            raise RuntimeError("Health server has already been started")
        self._prepare_socket_path()
        self._thread = threading.Thread(
            target=self._serve,
            name="mediasync-health",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout_seconds):
            raise HealthCheckError("Appliance health server did not start in time")
        if self._startup_error is not None:
            raise HealthCheckError(
                "Unable to start Appliance health server"
            ) from self._startup_error

    def stop(self, *, timeout_seconds: float = 5) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.2)
                client.connect(str(self._socket_path))
        except OSError:
            pass
        thread.join(timeout_seconds)
        if thread.is_alive():
            logger.error("appliance_health_server_stop_timeout")
        self._thread = None
        self._unlink_socket()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _prepare_socket_path(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._socket_path.exists() and not self._socket_path.is_symlink():
            return
        try:
            mode = self._socket_path.lstat().st_mode
        except OSError as exc:
            raise HealthCheckError(
                f"Unable to inspect health socket path: {self._socket_path}"
            ) from exc
        if not stat.S_ISSOCK(mode):
            raise HealthCheckError(
                f"Health socket path is not a Unix socket: {self._socket_path}"
            )
        self._socket_path.unlink()

    def _serve(self) -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                self._server_socket = server
                server.bind(str(self._socket_path))
                self._socket_path.chmod(0o600)
                server.listen(4)
                server.settimeout(0.5)
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        connection, _address = server.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        if self._stop.is_set():
                            continue
                        self._send_status(connection)
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            logger.exception("appliance_health_server_failed")
        finally:
            self._server_socket = None

    def _send_status(self, connection: socket.socket) -> None:
        try:
            supplied = self._status_provider()
            status_payload = {
                name: bool(supplied.get(name, False))
                for name in REQUIRED_COMPONENTS
            }
            encoded = json.dumps(status_payload, separators=(",", ":")).encode("utf-8")
            connection.sendall(encoded + b"\n")
        except Exception:
            logger.exception("appliance_health_status_failed")

    def _unlink_socket(self) -> None:
        try:
            if self._socket_path.exists() or self._socket_path.is_symlink():
                self._socket_path.unlink()
        except OSError:
            logger.exception(
                "appliance_health_socket_cleanup_failed path=%s",
                self._socket_path,
            )


def read_launcher_status(
    socket_path: Path = DEFAULT_HEALTH_SOCKET_PATH,
    *,
    timeout_seconds: float = 2,
) -> dict[str, bool]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
            client.connect(str(socket_path))
            chunks: list[bytes] = []
            received_bytes = 0
            while received_bytes < MAX_HEALTH_RESPONSE_BYTES:
                chunk = client.recv(min(4096, MAX_HEALTH_RESPONSE_BYTES - received_bytes))
                if not chunk:
                    break
                chunks.append(chunk)
                received_bytes += len(chunk)
                if b"\n" in chunk:
                    break
    except OSError as exc:
        raise HealthCheckError("Unable to connect to Appliance health socket") from exc

    if received_bytes >= MAX_HEALTH_RESPONSE_BYTES:
        raise HealthCheckError("Appliance health response exceeded the size limit")

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HealthCheckError("Invalid Appliance health response") from exc
    if not isinstance(payload, dict):
        raise HealthCheckError("Appliance health response must be a JSON object")
    return {
        name: payload.get(name) is True
        for name in REQUIRED_COMPONENTS
    }


def probe_http(url: str, timeout_seconds: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            return 200 <= response.status < 400
    except (OSError, TimeoutError, urllib.error.URLError):
        return False


def collect_health_status(
    socket_path: Path = DEFAULT_HEALTH_SOCKET_PATH,
    *,
    timeout_seconds: float = 1,
    http_probe: HttpProbe = probe_http,
) -> dict[str, bool]:
    status = read_launcher_status(
        socket_path,
        timeout_seconds=timeout_seconds,
    )
    web_healthy = http_probe(
        DEFAULT_NGINX_HEALTH_URL,
        timeout_seconds,
    )
    status["api"] = status["api"] and web_healthy
    status["nginx"] = status["nginx"] and web_healthy
    return status


def is_healthy(status: Mapping[str, bool]) -> bool:
    return all(status.get(name) is True for name in REQUIRED_COMPONENTS)
