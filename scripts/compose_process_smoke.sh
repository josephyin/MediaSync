#!/bin/sh
set -eu

repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
project_name=${MEDIASYNC_SMOKE_PROJECT:-mediasync-process-smoke}
database_copy=${MEDIASYNC_SMOKE_DATABASE:-}

export SECRET_KEY=${SECRET_KEY:-compose-smoke-secret-key}
export CREDENTIAL_ENCRYPTION_KEY=${CREDENTIAL_ENCRYPTION_KEY:-compose-smoke-credential-key}
export ADMIN_USERNAME=${ADMIN_USERNAME:-admin}
export ADMIN_PASSWORD=${ADMIN_PASSWORD:-compose-smoke-password}
export MEDIASYNC_HTTP_PORT=${MEDIASYNC_HTTP_PORT:-18080}

compose() {
    docker compose \
        --project-directory "$repository_root" \
        -f "$repository_root/docker-compose.yml" \
        -p "$project_name" \
        "$@"
}

cleanup() {
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM
cleanup

compose build mediasync-migrate

if [ -n "$database_copy" ]; then
    if [ ! -f "$database_copy" ]; then
        echo "Copied database does not exist: $database_copy" >&2
        exit 2
    fi
    database_directory=$(CDPATH= cd -- "$(dirname -- "$database_copy")" && pwd)
    database_filename=$(basename -- "$database_copy")
    compose run --rm --no-deps \
        --volume "$database_directory:/source:ro" \
        --entrypoint cp \
        mediasync-migrate \
        "/source/$database_filename" \
        /data/mediasync.db
fi

compose up -d mediasync-api mediasync-scheduler mediasync-worker frontend

health_attempt=0
until compose exec -T mediasync-api python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/system/health')" \
    >/dev/null 2>&1
do
    health_attempt=$((health_attempt + 1))
    if [ "$health_attempt" -ge 30 ]; then
        compose logs --no-color mediasync-api
        echo "API did not become healthy within 30 seconds" >&2
        exit 1
    fi
    sleep 1
done

frontend_attempt=0
until compose exec -T frontend python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost/')" \
    >/dev/null 2>&1
do
    frontend_attempt=$((frontend_attempt + 1))
    if [ "$frontend_attempt" -ge 30 ]; then
        compose logs --no-color frontend
        echo "Frontend did not become healthy within 30 seconds" >&2
        exit 1
    fi
    sleep 1
done

if [ -n "$database_copy" ]; then
    compose exec -T mediasync-api alembic current | grep "0006_task_execution_data_model_v2"
    compose exec -T mediasync-api python -c \
        "from sqlalchemy import func, or_, select; from app.core.database import SessionLocal; from app.models import Task; session = SessionLocal(); count = session.scalar(select(func.count()).select_from(Task).where(Task.status == 'running', or_(Task.locked_by.is_(None), Task.lock_token.is_(None), Task.locked_at.is_(None), Task.lease_until.is_(None)))) or 0; session.close(); assert count == 0, f'legacy running tasks remain: {count}'"
fi

migrate_id=$(compose ps -a -q mediasync-migrate)
cutover_id=$(compose ps -a -q mediasync-cutover)
test -n "$migrate_id"
test -n "$cutover_id"
test "$(docker inspect -f '{{.State.ExitCode}}' "$migrate_id")" = "0"
test "$(docker inspect -f '{{.State.ExitCode}}' "$cutover_id")" = "0"

api_logs=$(compose logs --no-color mediasync-api)
scheduler_logs=$(compose logs --no-color mediasync-scheduler)
worker_logs=$(compose logs --no-color mediasync-worker)
frontend_logs=$(compose logs --no-color frontend)

echo "$api_logs" | grep "background_execution_mode_selected process=api mode=process"
echo "$scheduler_logs" | grep "scheduler_started"
echo "$worker_logs" | grep "worker_started"
echo "$frontend_logs" | grep -v "emerg" >/dev/null

expected_image=$(compose images -q mediasync-api)
for service_name in \
    mediasync-migrate \
    mediasync-cutover \
    mediasync-api \
    mediasync-scheduler \
    mediasync-worker \
    frontend
do
    test "$(compose images -q "$service_name")" = "$expected_image"
done

if echo "$api_logs" | grep -E "Scheduler started|legacy transfer" >/dev/null; then
    echo "Legacy executor log detected in process-mode API" >&2
    exit 1
fi

compose ps -a
