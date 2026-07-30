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

- Git Tag：`v0.2.0-rc.8`；
- GitHub Release：预发布；
- 发布提交：`7ccc9171b07570151404f5d1a36ac593c979f136`；
- main 分支持续集成成功；
- 容器镜像发布工作流成功，用时 5 分 32 秒；
- 镜像构建、Docker Hub/GHCR 推送和 Docker Hub 中文 Overview 更新均成功；
- Docker Hub 与 GHCR 的 `v0.2.0-rc.8`、`rc` 标签摘要一致：
  `sha256:548cd16a09e6817e53218548605089a6b0c8742c86e2fd9129294d49344a5aaa`；
- 四个标签均包含 `linux/amd64` 和 `linux/arm64`；
- Docker Hub 精确标签可以在无登录凭据的临时 Docker 配置中拉取；
- GHCR 精确标签可以在无登录凭据的临时 Docker 配置中读取多架构清单。

## 远程镜像验收

- 从 Docker Hub 匿名拉取 `josephyjq/mediasync:v0.2.0-rc.8` 成功，摘要与发布
  结果一致；
- 镜像只声明 `9090/tcp` 和 `/data`；
- 健康检查元数据为 30 秒周期、15 秒超时、60 秒启动宽限、3 次重试；
- 使用独立临时数据卷首次启动成功，容器状态为 `healthy`；
- Web 健康 API 返回 `{"status":"ok"}`；
- Launcher、Nginx、API、Scheduler 和 Worker 全部正常；
- OpenAPI 版本为 `0.2.0-rc.8`；
- 默认 `admin/admin` 登录返回 200，并声明支持在线修改密码；
- 在线修改密码成功后旧会话变为未认证，旧密码登录返回 401；
- 容器重启后旧密码仍返回 401，新密码登录返回 200；
- 验收完成后临时容器、数据卷和匿名 Docker 配置均已删除。
