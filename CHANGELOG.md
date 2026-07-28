# 变更日志

本项目的重要变更记录在此文件中。版本号遵循语义化版本。

## [0.2.0-rc.2] - 2026-07-28

这是 v0.2 可靠性基础的第二个候选版本，重点统一容器镜像，同时继续保持 API、Scheduler、Worker 等进程的职责边界。

### 改进

- 使用一个 OCI 镜像运行 API、Scheduler、Worker、数据库迁移、切换检查和 Web 前端六个服务。
- 同一次构建同时发布到 GHCR 和 Docker Hub，支持 `linux/amd64` 与 `linux/arm64`。
- Docker Compose 支持通过 `MEDIASYNC_IMAGE`、`MEDIASYNC_IMAGE_TAG` 和 `MEDIASYNC_HTTP_PORT` 配置镜像、版本及访问端口。
- Web 前端增加容器健康检查，Nginx 访问日志和错误日志直接输出到容器标准输出。
- 保留 API、Scheduler、Worker 独立进程，不使用进程管理器在单容器内混合运行。

### 兼容性

- 本版本不包含数据库模型、迁移或业务行为变化。
- 从 `v0.2.0-rc.1` 升级时，使用新的单镜像 Compose 配置重新拉取并启动服务即可。
- `v0.2.0-rc.1` 的前后端双镜像已同步至 Docker Hub，便于现有部署继续使用。

### 已知限制

- 本版本仍需继续完成长期稳定性观察。
- 阿里云盘 Web 私有接口属于实验能力，可能因上游接口变化或风控策略失效。
- SQLite 部署仍只支持一个 Worker，Worker 并发度固定为 1。

## [0.2.0-rc.1] - 2026-07-24

这是 v0.2 可靠性基础的首个候选版本，目标是在 NAS 上长期、无人值守运行。

### 新增

- API、Scheduler、Worker 独立进程拓扑。
- Task Engine v2：任务执行记录、原子领取、租约、心跳、恢复和锁令牌防护。
- 进程切换前数据库迁移与旧任务对账屏障。
- 阿里云盘私有接口扫码登录、账号编辑、OpenAPI 绑定和多目标盘目录选择。
- 分享订阅增量扫描、目录检查点、完整校验和请求频率保护。
- 文件记录、任务历史、运行日志和转存状态展示。

### 改进

- 转存任务幂等、失败重试、指数退避和远端结果对账。
- 分享订阅删除时正确处理关联任务历史。
- 文件记录页面的信息层级、状态说明和时间显示。
- 后端 UTC 时间在前端按浏览器本地时区显示。
- 项目架构文档、ADR、协作规范和运维手册统一为中文。

### 部署

- Docker Compose 默认运行单 API、单 Scheduler、单 Worker。
- 提供 `linux/amd64` 和 `linux/arm64` 的 GHCR 预构建镜像。
- SQLite 部署只支持一个 Worker，Worker 并发度固定为 1。

### 已知限制

- 本版本为候选版本，仍需完成八周稳定性观察。
- 阿里云盘 Web 私有接口属于实验能力，可能因上游接口变化或风控策略失效。
- Provider SDK v2、多云盘、多用户和 PostgreSQL 不在本版本范围内。

[0.2.0-rc.2]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.2
[0.2.0-rc.1]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.1
