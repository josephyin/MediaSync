# 进程拓扑切换运维手册

本手册用于把官方 Docker Compose 部署从旧版单进程执行器升级到 v0.2 进程拓扑。

受支持的拓扑为：

```text
mediasync-migrate
        |
        v
mediasync-cutover
        |
        +----------+-------------------+
        |          |                   |
        v          v                   v
mediasync-api  mediasync-scheduler  mediasync-worker
        |
        v
    frontend
```

SQLite 方案只支持一个 Scheduler 和一个 Worker。不得对这两个服务执行
`docker compose up --scale`。

## 升级

切换过程需要维护窗口。新旧拓扑之间不得采用滚动重启。

1. 停止现有服务，并确认不存在手动启动的 Scheduler 或 Worker：

   ```bash
   docker compose stop
   docker compose ps -a
   ```

2. 拉取新代码并构建后端镜像，但暂不启动服务：

   ```bash
   git pull
   docker compose build mediasync-migrate
   ```

3. 在持久卷中创建带时间戳的数据库备份：

   ```bash
   docker compose run --rm --no-deps --entrypoint sh mediasync-migrate -c \
     'set -eu; stamp=$(date +%Y%m%d-%H%M%S); mkdir -p /data/backups/$stamp; cp -a /data/mediasync.db* /data/backups/$stamp/'
   ```

   将应用提交和当前 Alembic 版本与备份一起记录。条件允许时，继续操作前应把
   备份复制到 NAS 数据卷之外。

4. 启动新拓扑：

   ```bash
   docker compose up -d --build --remove-orphans
   ```

   Compose 会先执行 `alembic upgrade head`，再执行
   `python -m app.reconcile`。只有两个一次性服务都以代码 `0` 退出后，API、
   Scheduler 和 Worker 才会启动。

5. 验证 barrier、服务状态和进程模式日志：

   ```bash
   docker compose ps -a
   docker compose logs mediasync-migrate mediasync-cutover
   docker compose logs mediasync-api mediasync-scheduler mediasync-worker
   ```

   预期结果：

   - `mediasync-migrate` 和 `mediasync-cutover` 以代码 `0` 退出；
   - API、Scheduler、Worker 和 frontend 正在运行；
   - API 日志包含 `process=api mode=process`；
   - Scheduler 日志包含 `scheduler_started`；
   - Worker 日志包含 `worker_started`；
   - 不得出现进程内 APScheduler 或旧转存轮询器的启动日志。

6. 手动触发一次扫描，确认 API 返回持久化任务，随后确认 Worker 执行该任务。
   同时确认 Scheduler 对一个到期订阅只创建一次任务。

在整个观察期内保留备份。

## 常驻进程启动前失败

如果迁移或对账失败，Compose 会阻止 API、Scheduler、Worker 和 frontend 启动。

```bash
docker compose logs mediasync-migrate mediasync-cutover
docker compose down
```

修正配置或恢复备份后再重试。不得手动绕过失败的一次性服务来启动常驻进程。

## 回滚

如果 v2 Worker 尚未执行任何任务，可以停止服务、恢复切换前备份，再以
`legacy` 模式运行此前具备兼容能力的版本。

如果 v2 Worker 已经执行过任务，不得让 v0.1 程序直接访问 v2 数据库。此时只能：

1. 对明确受该版本支持的数据库结构，以 `legacy` 模式运行具备兼容能力的版本；
   或
2. 停止全部服务并恢复切换前数据库备份。

恢复 SQLite 不会撤销远端云盘操作。重试转存前，必须先对备份后已保存的文件
进行对账。
