# 变更日志

本项目的重要变更记录在此文件中。版本号遵循语义化版本。

## [0.2.0-rc.3] - 2026-07-28

这是 v0.2 可靠性基础的第三个候选版本，重点把单镜像进一步变为普通 NAS 用户
可直接运行的单容器 Appliance。

### 新增

- 默认镜像入口自动完成持久化配置、数据库迁移、旧任务对账和常驻进程启动。
- 内置 Launcher 独立监管 Nginx、API、Scheduler 和 Worker。
- 首次启动自动生成并持久化签名密钥、凭证加密密钥和管理员密码。
- Unix Socket 本地状态接口和五组件 Docker 聚合健康检查。
- Docker 单容器、飞牛 fnOS、备份、升级和回滚中文文档。
- Tag 发布后自动同步 Docker Hub 中文 Overview。

### 改进

- 普通用户只需映射一个 Web 端口和一个 `/data` 目录即可部署。
- API、Scheduler 和 Worker 在单容器内仍保持独立操作系统进程和职责边界。
- 迁移或对账失败时 Worker 不会启动，关键进程崩溃时容器整体快速失败。
- `SIGTERM` 按 Nginx、Scheduler、Worker、API 顺序优雅停止。
- 自动生成的管理员密码只在首次启动时输出一次。
- `SESSION_COOKIE_SECURE` 与运行环境名称解耦，局域网 HTTP 默认可以正常登录。
- Appliance 和高级多容器 Compose 继续复用同一镜像和业务实现。

### 兼容性

- 本版本不包含数据库模型或迁移变化。
- 从 rc.2 Compose 升级时，第一次启动必须提供原有签名密钥、凭证加密密钥和
  管理员密码；Appliance 会把它们持久化到 `/data/config/runtime-secrets.json`。
- 数据库与运行时密钥必须作为一个整体备份和恢复。
- 新旧部署不得同时访问同一个 SQLite 数据库。

### 已知限制

- 本版本仍需在真实飞牛 fnOS 图形界面完成远端镜像安装验证。
- Docker 默认停止宽限只有 10 秒；推荐设置 120 秒停止超时。
- 阿里云盘 Web 私有接口属于实验能力，可能因上游接口变化或风控策略失效。
- SQLite 部署仍只支持一个 Scheduler 和一个 Worker，Worker 并发度固定为 1。

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

[0.2.0-rc.3]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.3
[0.2.0-rc.2]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.2
[0.2.0-rc.1]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.1
