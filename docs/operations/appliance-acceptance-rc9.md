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

本节在 Tag、GitHub Release 和双 Registry 镜像发布完成后回填。

## 远程镜像验收

本节在拉取 Docker Hub 精确标签并完成 TaskRun API 验收后回填。
