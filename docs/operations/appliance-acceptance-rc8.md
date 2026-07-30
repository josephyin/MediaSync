# v0.2.0-rc.8 工程质量与运维能力验收记录

- 日期：2026-07-30
- 目标版本：v0.2.0-rc.8
- 变更范围：持续集成质量门禁、管理员在线修改密码、备份恢复演练与发布资料

## 验收不变量

- API、Scheduler、Worker 和 Nginx 仍由 Launcher 作为独立进程监管；
- SQLite 部署仍只允许一个 Scheduler 和一个 Worker，Worker 并发度为 1；
- 镜像只暴露 `9090/tcp`，数据统一持久化到 `/data`；
- 在线修改密码只在默认单容器 Appliance 中开放；
- 修改密码后全部旧会话失效，新密码随 `/data` 持久化；
- 数据库与运行时密钥必须作为一个整体备份和恢复；
- 不修改数据库模型、迁移、Task Engine 或 Provider 行为；
- 精确镜像标签发布后不可覆盖。

## 发布前验证

- Ruff 通过；
- 后端 312 项测试通过；
- 前端生产构建通过；
- `uv lock --check --offline` 通过；
- Alembic 新库升级、降级和再次升级通过；
- 本地单镜像构建通过；
- 容器聚合健康状态为 `healthy`；
- Launcher、Nginx、API、Scheduler 和 Worker 全部正常；
- OpenAPI 版本为 `0.2.0-rc.8`；
- 在线修改密码后旧密码返回 401，旧会话变为未认证；
- 容器重启后旧密码仍返回 401，新密码登录返回 200；
- 备份恢复演练文档中的数据库、密钥和登录校验步骤可执行。

## 发布结果

本节在 Tag、GitHub Release 和双 Registry 镜像发布完成后回填：

- Git Tag；
- GitHub Release 类型与发布提交；
- GitHub Actions 运行结果；
- Docker Hub 与 GHCR 精确标签和 `rc` 标签摘要；
- `linux/amd64` 与 `linux/arm64` 清单；
- Docker Hub Overview 同步结果。

## 远程镜像验收

本节在拉取 Docker Hub 精确标签后回填：

- 匿名拉取和镜像摘要；
- 单一 `9090/tcp`、`/data` 和健康检查元数据；
- 全新数据卷启动与聚合健康状态；
- OpenAPI 版本；
- 在线修改密码；
- 容器重启后新密码登录和旧密码拒绝。
