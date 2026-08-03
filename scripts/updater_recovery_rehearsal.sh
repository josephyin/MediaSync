#!/bin/sh
set -eu

usage() {
    echo "用法: $0 <精确镜像标签或 digest> <docker|synology|fnos> <报告目录>" >&2
    exit 2
}

image=${1:-}
platform=${2:-}
report_directory=${3:-}

[ -n "$image" ] || usage
[ -n "$report_directory" ] || usage
case "$platform" in
    docker|synology|fnos) ;;
    *) usage ;;
esac

command -v docker >/dev/null 2>&1 || {
    echo "未找到 docker 命令" >&2
    exit 2
}
docker info >/dev/null 2>&1 || {
    echo "无法连接 Docker daemon" >&2
    exit 2
}
docker image inspect "$image" >/dev/null 2>&1 || {
    echo "本机不存在镜像: $image" >&2
    echo "请先由 NAS 镜像管理器拉取精确版本，演练脚本不会自动拉取镜像。" >&2
    exit 2
}

probe_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
owner="mediasync-updater-rehearsal-owner-$probe_id"
contender="mediasync-updater-rehearsal-contender-$probe_id"
released="mediasync-updater-rehearsal-released-$probe_id"
volume="mediasync-updater-rehearsal-$probe_id"

cleanup() {
    docker rm -f "$owner" "$contender" "$released" >/dev/null 2>&1 || true
    docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for resource in "$owner" "$contender" "$released"; do
    if docker container inspect "$resource" >/dev/null 2>&1; then
        echo "演练资源已存在，拒绝覆盖: $resource" >&2
        exit 2
    fi
done
if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "演练数据卷已存在，拒绝覆盖: $volume" >&2
    exit 2
fi

mkdir -p "$report_directory"
report_directory=$(CDPATH= cd -- "$report_directory" && pwd)
report_path="$report_directory/updater-recovery-$platform-$probe_id.json"

owner_program='import fcntl, os, pathlib, time
directory = pathlib.Path("/data/update")
directory.mkdir(parents=True, exist_ok=True)
handle = open(directory / "updater.lock", "a+")
fcntl.flock(handle, fcntl.LOCK_EX)
(directory / "rehearsal-owner").write_text(os.environ["MEDIASYNC_REHEARSAL_TOKEN"], encoding="utf-8")
crash = directory / "rehearsal-crash"
while True:
    if crash.exists():
        crash.unlink()
        os._exit(99)
    time.sleep(0.2)'

contender_program='import fcntl, pathlib
directory = pathlib.Path("/data/update")
handle = open(directory / "updater.lock", "a+")
try:
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(75)'

released_program='import fcntl, pathlib
directory = pathlib.Path("/data/update")
handle = open(directory / "updater.lock", "a+")
fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
(directory / "rehearsal-released").write_text("released", encoding="utf-8")'

docker volume create "$volume" >/dev/null
docker run -d \
    --name "$owner" \
    --restart unless-stopped \
    --no-healthcheck \
    --network none \
    --read-only \
    -e MEDIASYNC_REHEARSAL_TOKEN=initial \
    -v "$volume:/data" \
    "$image" \
    python -c "$owner_program" >/dev/null

attempt=0
until [ "$(docker inspect -f '{{.State.Running}}' "$owner")" = "true" ] \
    && [ "$(docker exec "$owner" sh -c 'cat /data/update/rehearsal-owner')" = "initial" ]
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker logs "$owner" >&2 || true
        echo "锁持有者未就绪" >&2
        exit 1
    fi
    sleep 1
done

set +e
docker run --name "$contender" --network none --read-only \
    --no-healthcheck \
    -v "$volume:/data" "$image" python -c "$contender_program" >/dev/null 2>&1
contender_exit=$?
set -e
if [ "$contender_exit" -ne 75 ]; then
    echo "并发 contender 未被 flock 拒绝，退出码: $contender_exit" >&2
    exit 1
fi
docker rm "$contender" >/dev/null

# Docker 仅在容器稳定运行一段时间后应用 restart policy。
sleep 11
docker exec "$owner" sh -c 'touch /data/update/rehearsal-crash' >/dev/null

attempt=0
until [ "$(docker inspect -f '{{.State.Running}}' "$owner")" = "true" ] \
    && [ "$(docker inspect -f '{{.RestartCount}}' "$owner")" -ge 1 ]
do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        docker inspect "$owner" >&2
        echo "持久 helper 未按 restart policy 恢复" >&2
        exit 1
    fi
    sleep 1
done

set +e
docker run --name "$contender" --network none --read-only \
    --no-healthcheck \
    -v "$volume:/data" "$image" python -c "$contender_program" >/dev/null 2>&1
restart_contender_exit=$?
set -e
if [ "$restart_contender_exit" -ne 75 ]; then
    echo "helper 重启后未重新持有 flock，退出码: $restart_contender_exit" >&2
    exit 1
fi
docker rm "$contender" >/dev/null

docker update --restart no "$owner" >/dev/null
test "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$owner")" = "no"
docker stop -t 1 "$owner" >/dev/null

docker run --name "$released" --network none --read-only \
    --no-healthcheck \
    -v "$volume:/data" "$image" python -c "$released_program" >/dev/null
test "$(docker inspect -f '{{.State.ExitCode}}' "$released")" = "0"
test "$(docker run --rm --no-healthcheck -v "$volume:/data:ro" "$image" sh -c 'cat /data/update/rehearsal-released')" = "released"

image_id=$(docker image inspect "$image" --format '{{.Id}}')
repo_digests=$(docker image inspect "$image" --format '{{json .RepoDigests}}')
engine_version=$(docker version --format '{{.Server.Version}}')
restart_count=$(docker inspect -f '{{.RestartCount}}' "$owner")
completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)

printf '%s\n' \
    '{' \
    "  \"schema_version\": 1," \
    "  \"platform\": \"$platform\"," \
    "  \"image\": \"$image\"," \
    "  \"image_id\": \"$image_id\"," \
    "  \"repo_digests\": $repo_digests," \
    "  \"docker_engine_version\": \"$engine_version\"," \
    "  \"completed_at\": \"$completed_at\"," \
    '  "checks": {' \
    '    "exclusive_flock": true,' \
    '    "helper_restart": true,' \
    '    "lock_reacquired_after_restart": true,' \
    '    "restart_policy_disarmed": true,' \
    '    "lock_released_after_stop": true,' \
    "    \"restart_count\": $restart_count" \
    '  }' \
    '}' > "$report_path"

echo "演练通过: $report_path"
