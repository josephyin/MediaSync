import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPOSITORY_ROOT / "docker-compose.yml"
BACKEND_SERVICES = {
    "mediasync-migrate",
    "mediasync-cutover",
    "mediasync-api",
    "mediasync-scheduler",
    "mediasync-worker",
}


@pytest.fixture(scope="module")
def compose_config() -> dict[str, object]:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    environment = os.environ | {
        "SECRET_KEY": "compose-contract-secret",
        "CREDENTIAL_ENCRYPTION_KEY": "compose-contract-credential",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "compose-contract-password",
    }
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "config",
            "--format",
            "json",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def service(
    compose_config: dict[str, object],
    name: str,
) -> dict[str, object]:
    services = compose_config["services"]
    assert isinstance(services, dict)
    selected = services[name]
    assert isinstance(selected, dict)
    return selected


def test_compose_defines_process_topology(
    compose_config: dict[str, object],
) -> None:
    services = compose_config["services"]
    assert isinstance(services, dict)
    assert set(services) == BACKEND_SERVICES | {"frontend"}

    assert service(compose_config, "mediasync-migrate")["command"] == [
        "alembic",
        "upgrade",
        "head",
    ]
    assert service(compose_config, "mediasync-cutover")["command"] == [
        "python",
        "-m",
        "app.reconcile",
    ]
    assert service(compose_config, "mediasync-scheduler")["command"] == [
        "python",
        "-m",
        "app.scheduler",
    ]
    assert service(compose_config, "mediasync-worker")["command"] == [
        "python",
        "-m",
        "app.worker",
    ]
    api_command = service(compose_config, "mediasync-api")["command"]
    assert isinstance(api_command, list)
    assert api_command[:2] == ["uvicorn", "app.main:app"]


def test_compose_enforces_migration_and_reconciliation_barriers(
    compose_config: dict[str, object],
) -> None:
    cutover_dependencies = service(compose_config, "mediasync-cutover")["depends_on"]
    assert cutover_dependencies == {
        "mediasync-migrate": {
            "condition": "service_completed_successfully",
            "required": True,
        }
    }
    for process_name in (
        "mediasync-api",
        "mediasync-scheduler",
        "mediasync-worker",
    ):
        dependencies = service(compose_config, process_name)["depends_on"]
        assert dependencies == {
            "mediasync-cutover": {
                "condition": "service_completed_successfully",
                "required": True,
            }
        }
    assert service(compose_config, "frontend")["depends_on"] == {
        "mediasync-api": {
            "condition": "service_healthy",
            "required": True,
        }
    }


def test_all_backend_services_share_process_mode_and_sqlite_volume(
    compose_config: dict[str, object],
) -> None:
    volumes: set[tuple[str, str]] = set()
    images: set[str] = set()
    for process_name in BACKEND_SERVICES:
        process = service(compose_config, process_name)
        environment = process["environment"]
        assert isinstance(environment, dict)
        assert environment["BACKGROUND_EXECUTION_MODE"] == "process"
        assert environment["DATABASE_URL"] == "sqlite:////data/mediasync.db"

        process_volumes = process["volumes"]
        assert isinstance(process_volumes, list)
        assert len(process_volumes) == 1
        volume = process_volumes[0]
        assert isinstance(volume, dict)
        volumes.add((str(volume["source"]), str(volume["target"])))
        images.add(str(process["image"]))

    assert volumes == {("mediasync-data", "/data")}
    assert images == {"mediasync-backend:local"}


def test_sqlite_topology_declares_one_scheduler_and_worker(
    compose_config: dict[str, object],
) -> None:
    for process_name in ("mediasync-scheduler", "mediasync-worker"):
        deploy = service(compose_config, process_name)["deploy"]
        assert isinstance(deploy, dict)
        assert deploy["replicas"] == 1


def test_backend_image_and_nginx_have_single_process_responsibilities() -> None:
    dockerfile = (REPOSITORY_ROOT / "backend" / "Dockerfile").read_text()
    nginx = (REPOSITORY_ROOT / "deploy" / "nginx.conf").read_text()

    assert 'CMD ["uvicorn", "app.main:app"' in dockerfile
    assert "alembic upgrade head && uvicorn" not in dockerfile
    assert "proxy_pass http://mediasync-api:8000;" in nginx
    assert "proxy_pass http://backend:8000;" not in nginx
