from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.appliance.health import (
    DEFAULT_NGINX_HEALTH_URL,
    REQUIRED_COMPONENTS,
    ApplianceHealthServer,
    HealthCheckError,
    collect_health_status,
    is_healthy,
    read_launcher_status,
)


def short_socket_path() -> Path:
    return Path.cwd() / f".health-{uuid4().hex[:12]}.sock"


def test_health_server_returns_all_required_component_states() -> None:
    socket_path = short_socket_path()
    server = ApplianceHealthServer(
        socket_path=socket_path,
        status_provider=lambda: {
            "launcher": True,
            "nginx": True,
            "api": True,
            "scheduler": True,
            "worker": False,
        },
    )

    server.start()
    try:
        status = read_launcher_status(socket_path)
    finally:
        server.stop()

    assert status == {
        "launcher": True,
        "nginx": True,
        "api": True,
        "scheduler": True,
        "worker": False,
    }
    assert not is_healthy(status)
    assert not socket_path.exists()


def test_missing_component_is_reported_as_unhealthy() -> None:
    socket_path = short_socket_path()
    server = ApplianceHealthServer(
        socket_path=socket_path,
        status_provider=lambda: {"launcher": True},
    )

    server.start()
    try:
        status = read_launcher_status(socket_path)
    finally:
        server.stop()

    assert status["launcher"]
    assert all(not status[name] for name in REQUIRED_COMPONENTS if name != "launcher")


def test_health_server_refuses_to_replace_regular_file() -> None:
    socket_path = short_socket_path()
    socket_path.write_text("do-not-delete", encoding="utf-8")
    server = ApplianceHealthServer(
        socket_path=socket_path,
        status_provider=lambda: {},
    )

    with pytest.raises(HealthCheckError, match="not a Unix socket"):
        server.start()

    assert socket_path.read_text(encoding="utf-8") == "do-not-delete"
    socket_path.unlink()


def test_collect_health_status_checks_api_through_nginx_once() -> None:
    socket_path = short_socket_path()
    server = ApplianceHealthServer(
        socket_path=socket_path,
        status_provider=lambda: {name: True for name in REQUIRED_COMPONENTS},
    )
    probed_urls: list[str] = []

    def http_probe(url: str, _timeout_seconds: float) -> bool:
        probed_urls.append(url)
        return True

    server.start()
    try:
        status = collect_health_status(
            socket_path,
            http_probe=http_probe,
        )
    finally:
        server.stop()

    assert status["api"]
    assert status["nginx"]
    assert probed_urls == [DEFAULT_NGINX_HEALTH_URL]
    assert is_healthy(status)


def test_failed_nginx_api_probe_marks_both_components_unhealthy() -> None:
    socket_path = short_socket_path()
    server = ApplianceHealthServer(
        socket_path=socket_path,
        status_provider=lambda: {name: True for name in REQUIRED_COMPONENTS},
    )

    server.start()
    try:
        status = collect_health_status(
            socket_path,
            http_probe=lambda _url, _timeout_seconds: False,
        )
    finally:
        server.stop()

    assert not status["api"]
    assert not status["nginx"]
    assert not is_healthy(status)


def test_read_launcher_status_fails_when_socket_is_absent(
    tmp_path: Path,
) -> None:
    with pytest.raises(HealthCheckError, match="Unable to connect"):
        read_launcher_status(tmp_path / "missing.sock", timeout_seconds=0.01)
