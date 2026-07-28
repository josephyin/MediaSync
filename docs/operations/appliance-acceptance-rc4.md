# v0.2.0-rc.4 飞牛镜像默认配置验收记录

- 日期：2026-07-28
- 目标版本：v0.2.0-rc.4
- 本地平台：Docker Desktop，Linux ARM64 容器
- 变更范围：镜像元数据、Appliance Web 端口、默认管理员密码和部署文档

## 1. 镜像元数据

本地构建镜像后已确认：

- 只声明 `8080/tcp`；
- 不声明内部 API 端口 `8000`；
- 声明 `/data` 持久化卷；
- 声明 `ADMIN_PASSWORD=admin`；
- 默认入口仍为 `python -m app.appliance`。

这组元数据用于让飞牛创建容器时默认显示：

- 一条 `8080` 端口映射；
- 容器存储路径 `/data`，宿主机路径由用户选择；
- 环境变量 `ADMIN_PASSWORD=admin`。

## 2. 运行验证

使用本地镜像创建临时容器并映射 `18080:8080`，已确认：

- 容器自动创建 `/data` 卷；
- 数据卷为读写模式；
- 容器进入 `healthy`；
- `GET /api/v1/system/health` 返回 `{"status":"ok"}`；
- 使用用户名 `admin`、密码 `admin` 可以登录；
- Launcher、Nginx、API、Scheduler 和 Worker 健康检查保持有效。

## 3. 自动化验证

- Ruff 通过；
- Appliance 健康检查、Compose 契约和运行时密钥共 29 个专项测试通过；
- Docker 镜像构建通过；
- 高级多容器 Compose 继续使用前端容器端口 `80`；
- 内部 API 继续监听 `8000`，但不再通过镜像元数据对外声明。

## 4. 安全约束

- 默认密码 `admin` 只用于受信任局域网的首次安装；
- 用户首次登录后必须设置强密码；
- 默认密码下不得把管理端口暴露到公网；
- `SECRET_KEY` 和 `CREDENTIAL_ENCRYPTION_KEY` 仍随机生成并写入 `/data`；
- 旧数据目录的密钥保护和拒绝静默轮换规则不变。

## 5. Tag 发布后验证

发布 `v0.2.0-rc.4` 后继续确认：

- Docker Hub 和 GHCR 同时包含 `linux/amd64`、`linux/arm64`；
- 两个仓库的精确标签和 `rc` 标签摘要一致；
- 未登录状态可以读取精确标签；
- Docker Hub 中文 Overview 已更新；
- 飞牛 fnOS 创建容器表单显示预期默认值。
