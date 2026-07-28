# v0.2.0-rc.3 Appliance 验收记录

- 日期：2026-07-28
- 目标版本：v0.2.0-rc.3
- 本地平台：Docker Desktop，Linux ARM64 容器
- 镜像：本地构建 `mediasync:appliance-pr-c`
- 设计依据：[单容器 Appliance 模式设计](../architecture/single-container-appliance-rc3.md)

## 1. 自动化验证

结果：

- Ruff 全量通过；
- 后端 298 个测试通过；
- Unix Socket 健康检查专项测试通过；
- Docker Compose 配置兼容性测试通过；
- Docker 镜像从最终源码成功构建。

测试覆盖：

- 迁移或对账屏障失败时不启动常驻进程；
- API 未健康时不启动 Nginx、Scheduler 和 Worker；
- 关键子进程退出时停止其余进程；
- 停止顺序为 Nginx、Scheduler、Worker、API；
- Worker 超时后强制结束；
- 初始管理员密码只输出一次；
- 密钥不写入日志；
- HTTP 与 HTTPS Cookie 配置分离；
- 健康状态缺少任一组件时判定失败。

## 2. 空目录首次启动

使用一个空宿主机目录映射到 `/data` 启动默认镜像入口。

已确认：

- 自动生成运行时密钥和管理员密码；
- 自动执行 Alembic 迁移；
- 自动执行旧任务对账；
- API 健康后才启动 Nginx、Scheduler 和 Worker；
- 容器进入 `healthy`；
- `/data` 中生成数据库和 `config/runtime-secrets.json`；
- 密钥文件权限为 `0600`。

健康输出：

```json
{"launcher":true,"nginx":true,"api":true,"scheduler":true,"worker":true}
```

## 3. Web 与进程边界

已确认：

- Nginx 首页响应 200；
- `/api/v1/system/health` 通过 Nginx 响应 200；
- 局域网 HTTP 登录成功；
- 默认登录 Cookie 包含 `HttpOnly` 和 `SameSite=lax`，不包含 `Secure`；
- Launcher、Uvicorn API、Scheduler、Worker 和 Nginx 是独立操作系统进程；
- 容器没有使用特权模式或 Docker Socket。

## 4. 停止与重启

向容器发送 `SIGTERM`，已确认日志中的停止顺序：

```text
Nginx → Scheduler → Worker → API
```

容器正常停止退出码为 `0`。使用相同 `/data` 重启后：

- 容器重新进入 `healthy`；
- 初始管理员密码日志累计仍只有一条；
- 已持久化密钥和数据库被继续使用。

## 5. 故障注入

在健康容器中强制结束 Worker，已确认：

- Launcher 检测到 Worker 退出；
- Nginx、Scheduler 和 API 被依次停止；
- 容器快速以非零状态退出；
- 没有保持“网页可访问但任务不执行”的半失效状态。

被信号终止的子进程退出码会转换为通用的 `128 + signal` 容器退出码。

## 6. rc.2 升级与回滚演练

使用显式多容器命令创建一个只有数据库、没有
`config/runtime-secrets.json` 的 rc.2 风格数据目录。

升级演练：

1. 第一次启动 Appliance 时提供原有 `SECRET_KEY`、
   `CREDENTIAL_ENCRYPTION_KEY` 和 `ADMIN_PASSWORD`；
2. 容器进入 `healthy`；
3. Appliance 创建持久化密钥文件；
4. 删除容器；
5. 不再提供三个环境变量，用相同 `/data` 重建；
6. 容器再次进入 `healthy`；
7. 初始密码没有重复输出。

回滚契约演练：

- 停止 Appliance；
- 使用显式 Compose 风格命令和原密钥读取同一 SQLite 数据库；
- Alembic 当前版本仍为 `0006_task_execution_data_model_v2 (head)`；
- rc.3 没有新增数据库迁移。

## 7. Tag 发布后验证

以下项目必须在 Tag 工作流完成后继续验证：

- Docker Hub `v0.2.0-rc.3` 的 `linux/amd64` 和 `linux/arm64` 清单；
- GHCR `v0.2.0-rc.3` 的 `linux/amd64` 和 `linux/arm64` 清单；
- 两个仓库的 `rc` 浮动标签；
- Docker Hub 中文 Overview 同步结果；
- 未登录状态下从 Docker Hub 和 GHCR 拉取精确标签；
- 飞牛 fnOS 图形界面的真实拉取、端口映射、目录映射和首次登录。

飞牛图形界面验证需要已发布的远端精确标签，因此作为 Release PR 合并后的发布
验收项执行，不用本地临时镜像代替。
