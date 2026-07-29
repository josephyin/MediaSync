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

## 发布后验证

Tag 发布后继续确认：

- Docker Hub 与 GHCR 的 `v0.2.0-rc.7`、`rc` 多架构摘要一致；
- 两个仓库均包含 `linux/amd64` 和 `linux/arm64`；
- 未登录状态可以读取精确标签；
- 远程镜像只声明 `9090/tcp` 和 `/data`；
- 远程容器映射 `9090:9090` 后可以变为健康；
- Docker Hub 中文 Overview 已更新为 rc.7 和端口 `9090`。
