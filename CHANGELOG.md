# 变更日志

本项目的重要变更记录在此文件中。版本号遵循语义化版本。

## [0.2.0-rc.6] - 2026-07-28

这是 v0.2 可靠性基础的第六个候选版本，重点修复低性能 NAS 上 Docker 健康
检查容易超时的问题，并加入正式 MediaSync 品牌图标和群晖安装说明。

### 新增

- 新增播放与同步语义结合的 MediaSync SVG 品牌图标。
- 浏览器 favicon、登录页和管理后台侧边栏统一使用正式图标。
- 新增群晖 DSM Container Manager 中文安装教程。
- 明确群晖不会自动创建宿主机 `/data` 文件夹映射，创建容器时必须手工添加。

### 修复

- Docker 健康检查超时从 5 秒调整为 15 秒。
- 启动宽限从 30 秒调整为 60 秒，适应启动较慢的 NAS。
- 健康检查改为通过 Nginx 代理检查 API，一次请求同时覆盖 Web 入口与 API，
  避免重复串行探测导致假超时。
- 保留 Launcher、Nginx、API、Scheduler 和 Worker 五组件健康语义。

### 兼容性

- 本版本不包含数据库、迁移、Task Engine 或 Provider 行为变化。
- 端口、`/data`、默认管理员密码和已有密码升级保护契约不变。
- 从 rc.5 升级时复用原有 `/data`，无需额外迁移步骤。

## [0.2.0-rc.5] - 2026-07-28

这是 v0.2 可靠性基础的第五个候选版本，保留 rc.4 的飞牛友好镜像默认值，并
修复已有数据目录升级时管理员密码可能被默认值覆盖的问题。

### 修复

- 新增 `IMAGE_DEFAULT_ADMIN_ONLY=true` 镜像标记。
- 全新 `/data` 仍默认使用用户名 `admin`、密码 `admin`。
- 已有 `/data` 会忽略镜像自带的 `admin` 默认值，继续保留原管理员密码。
- 用户显式填写其他 `ADMIN_PASSWORD` 时仍可离线重置密码。
- 增加旧密码保留和自定义密码覆盖测试。

### 安全说明

- rc.4 不建议用于已有数据目录升级；请直接使用 rc.5。
- 默认密码仅用于全新安装，首次登录后必须改为强密码。
- 加密密钥生成、持久化和拒绝静默轮换规则不变。

## [0.2.0-rc.4] - 2026-07-28

这是 v0.2 可靠性基础的第四个候选版本，重点优化飞牛 fnOS 等图形化 Docker
平台的首次安装默认值。

### 改进

- Appliance Web 入口统一使用容器端口 `8080`。
- 镜像只声明一个 `8080/tcp`，不再让图形界面自动生成内部 API `8000` 映射。
- 镜像声明 `/data` 持久化卷，飞牛创建容器时可以自动显示容器路径。
- `ADMIN_PASSWORD` 默认值为 `admin`，图形界面无需手工补齐即可首次登录。
- 健康检查改为探测 Appliance 的 `8080` 入口。
- 更新 Docker、飞牛和 Docker Hub 中文安装说明。

### 安全说明

- 默认用户名和密码均为 `admin`，只用于受信任局域网的首次安装。
- 首次登录后必须通过 `ADMIN_PASSWORD` 设置自己的强密码并重建容器。
- 使用默认密码时不得把管理端口暴露到公网。
- 加密密钥仍由 Appliance 随机生成并持久化，不使用固定默认值。

### 兼容性

- 本版本不包含数据库模型、迁移、Task Engine 或 Provider 行为变化。
- 单容器部署从 `8080:80` 改为 `8080:8080`。
- 高级多容器 Compose 继续使用宿主机 `8080` 到前端容器 `80` 的映射，职责和
  内部端口契约不变。

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

[0.2.0-rc.6]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.6
[0.2.0-rc.5]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.5
[0.2.0-rc.4]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.4
[0.2.0-rc.3]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.3
[0.2.0-rc.2]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.2
[0.2.0-rc.1]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.1
