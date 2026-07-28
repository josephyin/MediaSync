# v0.2.0-rc.2 单镜像部署设计

- 状态：提议中
- 目标版本：v0.2.0-rc.2
- 范围：容器构建、Compose 镜像契约、双 Registry 发布、升级与回滚
- 依据：[ADR-0003：单一发布镜像，多进程容器拓扑](adr/ADR-0003-single-release-image.md)
- 最后更新：2026-07-28

## 1. 背景

v0.2.0-rc.1 发布两个 OCI 镜像：

```text
mediasync-backend
├── migrate
├── cutover
├── api
├── scheduler
└── worker

mediasync-frontend
└── nginx + Vue dist
```

后端五个服务已经复用同一个镜像，但用户仍需配置、拉取和核对两个镜像。对于
NAS 用户，这增加了部署参数和发布产物数量，也使版本不一致更容易发生。

rc.2 计划把后端运行时、Nginx 和前端静态资源放入同一个 OCI 镜像。Compose
仍然启动相互独立的容器，并通过不同命令选择职责。

单镜像只统一发布产物，不重新合并运行时进程。

## 2. 目标

rc.2 必须：

- 每个版本只发布一个 `mediasync` OCI 镜像；
- 保持 API、Scheduler、Worker 的进程和职责边界；
- 保持迁移、对账两个一次性启动屏障；
- 保持 SQLite 单 Scheduler、单 Worker、并发度 1 的限制；
- 在 GHCR 和 Docker Hub 发布同一源码构建的多架构产物；
- 支持 `linux/amd64` 和 `linux/arm64`；
- 简化 Compose 镜像配置，避免前后端版本不一致；
- 提供从 rc.1 双镜像部署升级和回滚的确定步骤。

## 3. 非目标

本设计不包含：

- 把 API、Scheduler、Worker 和 Nginx 放进同一个容器；
- 引入 Supervisor、systemd 或其他容器内进程管理器；
- 修改 Task Engine、Provider、Credential 或数据库模型；
- 修改 SQLite 单 Worker 决策；
- 新增 Provider、媒体生态集成或业务功能；
- 把 Vue 静态文件改由 FastAPI 直接提供；
- 覆盖或重打 `v0.2.0-rc.1`。

## 4. 核心不变量

### 4.1 一个产物不等于一个进程

所有服务可以引用同一个镜像，但每个常驻容器只能运行一个主进程：

| 服务 | 容器主命令 |
|---|---|
| `mediasync-migrate` | `alembic upgrade head` |
| `mediasync-cutover` | `python -m app.reconcile` |
| `mediasync-api` | `uvicorn app.main:app ...` |
| `mediasync-scheduler` | `python -m app.scheduler` |
| `mediasync-worker` | `python -m app.worker` |
| `frontend` | `nginx -g 'daemon off;'` |

镜像不得通过环境变量隐式启动多个进程，也不得增加会同时拉起多个服务的默认
入口脚本。

### 4.2 发布原子性

一个版本的所有服务必须引用同一个精确镜像标签和 OCI 索引。Compose 不再允许
分别配置前端、后端版本。

精确版本标签发布后不可覆盖。`rc` 和 `latest` 只是可移动别名，不得作为生产
回滚依据。

### 4.3 启动顺序不变

单镜像不得改变以下屏障：

```text
migrate
   ↓
cutover
   ↓
api + scheduler + worker
   ↓
frontend 等待 API 健康
```

迁移或对账失败时，常驻服务仍然必须被 Compose 阻止启动。

### 4.4 数据契约不变

rc.2 单镜像改造不得增加 Alembic 迁移，不得修改 SQLite 数据格式。升级失败时，
用户可以停止 rc.2 并重新使用 rc.1 双镜像 Compose；远端云盘操作仍需按既有
运维手册对账。

## 5. 镜像构建

### 5.1 多阶段结构

建议采用三阶段 Dockerfile：

```text
frontend-builder
    node:22-alpine
    npm ci
    npm run build

backend-builder
    python:3.12-slim
    构建 mediasync-backend wheel

runtime
    python:3.12-slim
    安装 nginx 和后端 wheel
    复制 migrations、alembic.ini、Vue dist、nginx.conf
```

最终镜像必须包含：

- Python 3.12 运行时和后端依赖；
- Alembic 配置与迁移；
- Nginx；
- Vue 生产静态文件；
- Nginx 反向代理配置。

不得把 `node_modules`、源码构建缓存、测试数据、SQLite 数据库或真实 `.env`
复制到最终镜像。

### 5.2 入口契约

Dockerfile 可以保留 API 作为默认命令，方便单独运行和检查；官方 Compose 必须
为六个服务显式声明命令，不依赖默认命令推断职责。

所有服务必须使用相同的工作目录和不可变应用文件。只有 `/data` 是持久化写入
位置。

### 5.3 镜像大小与安全

单镜像会让 Scheduler、Worker 等容器携带未使用的 Nginx 和静态资源，这是换取
单一发布产物的明确成本。

实现 PR 必须记录 rc.1 两个镜像与 rc.2 单镜像的压缩大小。若单镜像超过 rc.1
两个镜像压缩体积之和的 120%，必须回到设计评审。

本次改造不得扩大容器权限、挂载 Docker Socket 或增加特权模式。若当前 Nginx
和后端运行用户无法统一，必须在实现 PR 中明确记录，不能用特权容器绕过。

## 6. Compose 契约

rc.2 使用两个镜像变量：

```dotenv
MEDIASYNC_IMAGE=ghcr.io/josephyin/mediasync
MEDIASYNC_IMAGE_TAG=v0.2.0-rc.2
```

所有服务引用：

```yaml
image: ${MEDIASYNC_IMAGE}:${MEDIASYNC_IMAGE_TAG}
```

rc.1 的 `MEDIASYNC_BACKEND_IMAGE` 和 `MEDIASYNC_FRONTEND_IMAGE` 在 rc.2 官方
Compose 中移除，不保留隐式回退。升级文档必须明确要求替换环境变量。

本地开发仍可以通过 Compose 的 `build` 配置构建同一个镜像。正式部署使用：

```bash
docker compose pull
docker compose up -d --no-build
```

## 7. Registry 与标签

同一次构建必须推送到：

```text
ghcr.io/josephyin/mediasync:<tag>
docker.io/<DOCKERHUB_USERNAME>/mediasync:<tag>
```

候选版本标签：

```text
v0.2.0-rc.2
rc
```

正式版本标签：

```text
v0.2.0
latest
```

GitHub Actions 必须从一个 Buildx 构建结果同时推送两个 Registry，避免为同一
版本分别构建。发布完成后必须在未登录环境验证两个 Registry 的架构清单。

## 8. 健康检查与可观测性

镜像统一后，各服务仍保留独立健康或运行状态：

- API 继续检查 `/api/v1/system/health`；
- frontend 检查 Nginx HTTP 响应；
- Scheduler 和 Worker 继续通过进程退出码与结构化日志反映状态；
- migrate 和 cutover 继续使用一次性容器退出码。

不能用“镜像存在”代替进程健康，也不能因为镜像相同就合并日志来源。

## 9. 升级

从 rc.1 升级到 rc.2：

1. 停止现有 Compose 服务；
2. 备份 `/data/mediasync.db*`；
3. 把两个镜像变量替换为 `MEDIASYNC_IMAGE`；
4. 把标签设置为 `v0.2.0-rc.2`；
5. 拉取单镜像；
6. 启动 Compose；
7. 验证迁移、对账、API、Scheduler、Worker 和 frontend。

本次升级不应执行新的数据库迁移，但仍保留迁移屏障，以保证部署流程一致。

## 10. 回滚

回滚到 rc.1：

1. 停止 rc.2 全部服务；
2. 恢复 rc.1 对应的 Compose 文件和两个镜像变量；
3. 使用精确标签 `v0.2.0-rc.1`；
4. 启动并验证所有服务；
5. 对 rc.2 运行期间发生的远端转存执行对账。

不得通过把 `rc` 标签指回旧版本来代替显式回滚。

## 11. 验收标准

实现 PR 合并前必须证明：

- 单个 Dockerfile 能构建 `amd64`、`arm64`；
- 六个 Compose 服务解析为同一个镜像引用；
- migrate 和 cutover 屏障仍然有效；
- API 健康检查通过；
- 前端可加载并能访问 `/api`；
- Scheduler 只创建任务，不执行扫描；
- Worker 是唯一任务执行者；
- SQLite 仍然只有一个 Scheduler 和一个 Worker；
- 容器重启后任务恢复测试通过；
- GHCR 和 Docker Hub 的精确版本均可匿名拉取；
- 两个 Registry 均包含 `amd64`、`arm64`；
- rc.1 到 rc.2 的升级与回滚说明经过 Compose 演练；
- 不新增数据库迁移和业务功能。

## 12. 实现顺序

设计 PR 合并后按以下顺序执行：

1. 创建单镜像实现 Issue，引用本设计和 ADR-0003；
2. 实现多阶段 Dockerfile 和 Compose 单镜像契约；
3. 增加 Compose、镜像内容和进程边界测试；
4. 更新运维手册和 `.env.example`；
5. 完成 rc.1 到 rc.2 的本地升级与回滚演练；
6. 创建独立 Release PR，更新版本号和变更日志；
7. 打 `v0.2.0-rc.2` 标签并发布到 GHCR、Docker Hub；
8. 匿名验证两个 Registry 后再宣布发布完成。

运行时 PR 不得与本设计 PR 合并为一个大改动。
