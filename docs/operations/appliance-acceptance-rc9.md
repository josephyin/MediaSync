# v0.2.0-rc.9 任务执行信息修复验收记录

- 日期：2026-07-31
- 目标版本：v0.2.0-rc.9
- 变更范围：任务中心 TaskRun 执行信息投影与状态语义

## 验收不变量

- Task Engine v2 的任务状态机、领取、租约、恢复和 fencing 行为保持不变；
- `tasks` 和 `task_runs` 数据模型及 Alembic 版本保持不变；
- API、Scheduler、Worker 和 Nginx 进程边界保持不变；
- SQLite 部署仍只允许一个 Scheduler 和一个 Worker；
- 镜像只暴露 `9090/tcp`，数据统一持久化到 `/data`；
- 精确镜像标签发布后不可覆盖。

## 功能验收

- 已有终态 Task 的最新 TaskRun 执行摘要可以在任务列表显示；
- 任务详情显示最新执行批次、开始时间和结束时间；
- 重试次数来自 `retry_count/max_retries`；
- 成功、最终失败、取消、等待执行、执行中、等待重试和等待凭证的下次尝试语义
  明确；
- SQLite 返回的无时区 UTC 时间仍由前端转换为浏览器本地时间；
- 没有 TaskRun 的待执行任务显示“尚未执行”，不会伪造执行时间。

## 发布前验证

- Ruff 通过；
- 后端 313 项测试通过；
- 前端生产构建通过；
- `uv lock --check --offline` 通过；
- Alembic 新库升级、检查、降级和再次升级通过；
- 本地单镜像构建通过；
- 容器聚合健康状态为 `healthy`；
- OpenAPI 版本为 `0.2.0-rc.9`；
- 在容器数据库中构造已有 TaskRun 后，任务列表 API 返回执行摘要和开始/结束时间。

## 发布结果

- 发布提交：`8992d76698561ce1127ea53a1a28faeb7a6f2a52`；
- Git Tag：`v0.2.0-rc.9`；
- GitHub Release：已创建预发布版本；
- 镜像工作流：`30598302777`，成功尝试耗时 5 分 8 秒；
- 首次执行因 runner 长时间无终态被取消，第二次执行因 npm 网络
  `ECONNRESET` 失败，第三次执行成功；
- Docker Hub 中文说明自动同步成功；
- Docker Hub 与 GHCR 的精确标签和 `rc` 标签均指向：
  `sha256:07dc2dd17004df584d0cd5ccf8d8d0b3a804d35ad430f43815d87a19d859bffc`；
- 四个标签均包含 `linux/amd64` 和 `linux/arm64` 镜像；
- Docker Hub 镜像可在无登录配置下匿名拉取；
- GHCR 镜像清单可在无登录配置下匿名读取。

## 远程镜像验收

- 验收镜像：`josephyjq/mediasync:v0.2.0-rc.9`；
- 宿主端口映射：`19119:9090`，公共健康 API 返回 `{"status":"ok"}`；
- 容器健康检查确认 `launcher`、`nginx`、`api`、`scheduler`、`worker`
  五个组件全部健康；
- OpenAPI 版本为 `0.2.0-rc.9`；
- 默认管理员账号可以登录；
- 在临时数据卷中写入一个终态 Task 和对应 TaskRun 后，任务列表 API 返回：
  - 执行摘要：`远端 rc.9 执行信息验收通过`；
  - `started_at` 和 `finished_at` 均有值；
  - `retry_count=1`、`max_retries=3`；
  - `next_attempt_at=null`，符合成功任务无需再次尝试的语义；
  - `latest_run.run_number=2`、`duration_ms=45000`；
- 验收结束后已删除临时容器和临时数据卷。
