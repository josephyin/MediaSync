# v0.2.0-rc.6 NAS 健康检查与品牌图标验收记录

- 日期：2026-07-28
- 目标版本：v0.2.0-rc.6
- 变更范围：NAS 健康检查容错、MediaSync 品牌图标、群晖部署说明

## 验收不变量

- API、Scheduler、Worker 和 Nginx 仍由 Launcher 作为独立进程监管；
- 健康检查仍覆盖 Launcher、Nginx、API、Scheduler 和 Worker；
- 容器只暴露 `8080/tcp`；
- 数据仍统一持久化到 `/data`；
- 全新安装默认 `admin/admin`，已有数据目录不会被默认密码覆盖；
- 不修改数据库、迁移、Task Engine 或 Provider 行为。

## 发布前验证

- Ruff 通过；
- 后端 301 项测试通过；
- 前端生产构建通过；
- 本地单镜像构建通过；
- 本地容器连续 5 次健康检查通过，单次耗时约 0.3 至 0.4 秒；
- 镜像健康检查元数据为 30 秒周期、15 秒超时、60 秒启动宽限、3 次重试；
- Logo 静态资源返回 HTTP 200 且与仓库 SVG 一致；
- Web 健康 API 返回 HTTP 200。

## 发布结果

- Git Tag：`v0.2.0-rc.6`；
- GitHub Release：预发布；
- 发布提交：`7147ead3a7fbfb05300af52cb1d14766e8a29fc3`；
- GitHub Actions：第 1 次构建在 `linux/arm64` 前端依赖安装阶段长时间无进展，人工取消后重新执行；第 2 次构建成功；
- Docker Hub 与 GHCR 的 `v0.2.0-rc.6`、`rc` 标签摘要一致：
  `sha256:4c1b8693f60306b2a1f1c14c1cdcfad7cebb479d80f762c879a04c45ff283bb4`；
- 四个标签均包含 `linux/amd64` 和 `linux/arm64`；
- 在不使用登录凭据的临时 Docker 配置下，Docker Hub 与 GHCR 的精确标签均可读取；
- Docker Hub 中文 Overview 已由发布工作流成功更新。

## 远程镜像验收

- 从 Docker Hub 拉取 `josephyjq/mediasync:v0.2.0-rc.6` 成功，摘要与发布结果一致；
- 镜像仅暴露 `8080/tcp`，声明持久化目录 `/data`；
- 默认环境变量包含 `ADMIN_PASSWORD=admin` 和 `IMAGE_DEFAULT_ADMIN_ONLY=true`；
- 健康检查元数据为 30 秒周期、15 秒超时、60 秒启动宽限、3 次重试；
- 使用独立临时数据卷首次启动成功，容器状态为 `healthy`；
- 健康检查连续返回 Launcher、Nginx、API、Scheduler 和 Worker 全部正常；
- Web 健康 API 返回 `{"status":"ok"}`；
- OpenAPI 版本为 `0.2.0-rc.6`；
- 容器内 Logo 与仓库 SVG 的 SHA-256 均为
  `146c8fa88a66751a591c759939ac2df2d81d8ee12542e1f1cd622d4aa9a226d9`。
