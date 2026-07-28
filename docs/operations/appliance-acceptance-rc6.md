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

## 发布后验证

Tag 发布后继续确认：

- Docker Hub 与 GHCR 的 `v0.2.0-rc.6`、`rc` 多架构摘要一致；
- 两个仓库均包含 `linux/amd64` 和 `linux/arm64`；
- 未登录状态可以读取精确标签；
- 远程镜像健康检查参数与发布前验证一致；
- 远程容器可以变为健康；
- 远程镜像包含并正确提供 MediaSync Logo；
- Docker Hub 中文 Overview 已更新。
