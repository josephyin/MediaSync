# v0.2.0-rc.7 默认端口 9090 验收记录

- 日期：2026-07-29
- 目标版本：v0.2.0-rc.7
- 变更范围：Appliance、Docker Compose 和当前部署文档的默认 Web 端口

## 验收不变量

- API、Scheduler、Worker 和 Nginx 仍由 Launcher 作为独立进程监管；
- 健康检查仍覆盖 Launcher、Nginx、API、Scheduler 和 Worker；
- 镜像只暴露一个 Web 端口；
- 数据仍统一持久化到 `/data`；
- 全新安装默认 `admin/admin`，已有数据目录不会被默认密码覆盖；
- 不修改数据库、迁移、Task Engine 或 Provider 行为；
- 精确镜像标签发布后不可覆盖。

## 端口契约

- Appliance Nginx 监听 `9090`；
- 镜像声明 `9090/tcp`；
- Docker Hub 和 NAS 图形化安装默认映射为 `9090:9090`；
- 高级 Docker Compose 默认使用宿主机端口 `9090`；
- 内部 API 继续使用 `8000`，不向普通用户暴露；
- 从 rc.6 升级时必须重建端口映射，原 `/data` 保持不变。

## 发布前验证

- Ruff 通过；
- 后端 301 项测试通过；
- 前端生产构建通过；
- `uv lock --check --offline` 通过；
- Compose 展开配置的默认宿主机端口为 `9090`；
- 本地单镜像构建通过，镜像只声明 `9090/tcp` 和 `/data`；
- 容器内 Nginx 的 `9090` 健康入口返回 `{"status":"ok"}`；
- 使用临时宿主机端口映射到容器 `9090` 后，Web 健康 API 正常；
- 容器聚合健康状态为 `healthy`，Launcher、Nginx、API、Scheduler 和 Worker
  全部正常；
- OpenAPI 版本为 `0.2.0-rc.7`；
- Logo 静态资源 SHA-256 为
  `146c8fa88a66751a591c759939ac2df2d81d8ee12542e1f1cd622d4aa9a226d9`。

## 发布结果

- Git Tag：`v0.2.0-rc.7`；
- GitHub Release：预发布；
- 发布提交：`37fcd78539b2c81858d99d11432804a078847026`；
- GitHub Actions 第 1 次构建成功，用时约 5 分 44 秒；
- 镜像构建、Docker Hub/GHCR 推送和 Docker Hub 中文 Overview 更新均成功；
- Docker Hub 与 GHCR 的 `v0.2.0-rc.7`、`rc` 标签摘要一致：
  `sha256:99fa8cf7667784ccd9ac8cb6e09b599b440e701b5ee36722651be62f942556a0`；
- 四个标签均包含 `linux/amd64` 和 `linux/arm64`；
- 在不使用登录凭据的临时 Docker 配置下，Docker Hub 与 GHCR 的精确标签均可读取。

## 远程镜像验收

- 从 Docker Hub 拉取 `josephyjq/mediasync:v0.2.0-rc.7` 成功，摘要与发布结果一致；
- 镜像只声明 `9090/tcp` 和 `/data`；
- 默认环境变量包含 `ADMIN_PASSWORD=admin` 和 `IMAGE_DEFAULT_ADMIN_ONLY=true`；
- 健康检查元数据为 30 秒周期、15 秒超时、60 秒启动宽限、3 次重试；
- 使用独立临时数据卷首次启动成功，容器状态为 `healthy`；
- 宿主机临时端口映射到容器 `9090` 后，Web 健康 API 返回 `{"status":"ok"}`；
- Launcher、Nginx、API、Scheduler 和 Worker 全部正常；
- OpenAPI 版本为 `0.2.0-rc.7`；
- 容器内 Logo SHA-256 为
  `146c8fa88a66751a591c759939ac2df2d81d8ee12542e1f1cd622d4aa9a226d9`。
