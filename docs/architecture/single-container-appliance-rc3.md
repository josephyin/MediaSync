# v0.2.0-rc.3 单容器 Appliance 模式设计

- 状态：草案
- 目标版本：v0.2.0-rc.3
- 范围：默认容器入口、进程监管、首次启动、持久化配置、健康检查和部署兼容性
- 依据：[ADR-0004：默认单容器 Appliance，保留多进程职责边界](adr/ADR-0004-single-container-appliance.md)
- 取代的默认部署设计：[v0.2.0-rc.2 单镜像部署设计](single-image-deployment-rc2.md)
- 最后更新：2026-07-28

## 1. 背景

v0.2.0-rc.2 把后端运行时、Nginx 和前端静态资源统一到一个 OCI 镜像，但官方
部署仍要求 Compose 启动六个容器：

```text
mediasync-migrate
mediasync-cutover
mediasync-api
mediasync-scheduler
mediasync-worker
frontend
```

该拓扑适合验证进程边界，却不符合普通 NAS 用户对 Docker Hub 镜像的预期。
飞牛、群晖、威联通等平台的常见安装路径是：

1. 搜索并下载一个镜像；
2. 映射一个 Web 端口；
3. 映射一个持久化目录；
4. 点击启动。

用户不应该为了运行 MediaSync 理解迁移容器、对账容器、服务依赖和 Compose
锚点。单一发布镜像如果仍然只能通过多容器编排使用，就只减少了维护者的发布
产物，没有充分降低普通用户的部署成本。

本设计引入默认 **Appliance 模式**：一个容器内运行相互独立的 Nginx、API、
Scheduler 和 Worker 进程，由一个只负责生命周期的 Launcher 监管。高级用户
仍可通过显式命令使用 rc.2 的多容器拓扑。

## 2. 目标

rc.3 必须实现：

- 用户仅通过 Docker Hub 镜像、一个端口和一个 `/data` 目录即可完成首次启动；
- 默认镜像入口自动执行数据库迁移和旧任务对账；
- 默认入口启动 Nginx、API、一个 Scheduler 和一个 Worker；
- API、Scheduler、Worker 继续保持独立进程和职责边界；
- Launcher 不执行扫描、转存、调度或 Provider 调用；
- 未显式提供的运行时密钥在首次启动时安全生成并持久化到 `/data`；
- 容器重建、NAS 重启和镜像升级后复用同一份数据库与密钥；
- 任一关键子进程异常退出时，整个容器以非零状态退出，由 Docker 重启策略恢复；
- 保留高级多容器部署所需的显式服务命令；
- 继续发布 `linux/amd64` 和 `linux/arm64` 镜像。

普通用户的目标安装契约为：

```bash
docker run -d \
  --name mediasync \
  -p 8080:80 \
  -v /path/to/mediasync:/data \
  --restart unless-stopped \
  josephyjq/mediasync:v0.2.0-rc.3
```

用户可以通过 `ADMIN_PASSWORD` 覆盖自动生成的初始管理员密码：

```bash
-e ADMIN_PASSWORD='用户设置的强密码'
```

## 3. 非目标

本设计不包含：

- 把 Scheduler 或 Worker 代码重新放入 FastAPI 生命周期；
- 允许 API 直接执行扫描或转存；
- 修改 Task Engine 状态机、租约、fencing 或幂等语义；
- 支持多个 Scheduler、多个 Worker 或 Worker 并发度大于 1；
- 引入 Redis、消息队列、PostgreSQL、Kubernetes 或 Docker Socket；
- 修改 Provider、分享扫描或转存业务行为；
- 新增云盘 Provider、STRM 生成或媒体生态集成；
- 用单容器模式取代高级多容器模式；
- 提供完整的 Web 首次安装向导；
- 在 rc.3 内实现在线修改管理员密码或密钥轮换。

## 4. 支持的部署模式

### 4.1 默认 Appliance 模式

镜像没有显式命令时进入 Appliance 模式：

```text
container PID 1: MediaSync Launcher
├── nginx
├── uvicorn
├── scheduler
└── worker
```

迁移和对账是启动屏障，不是常驻子进程：

```text
准备 /data 与运行时配置
          ↓
alembic upgrade head
          ↓
python -m app.reconcile
          ↓
启动 API 并等待健康
          ↓
启动 Nginx + Scheduler + Worker
          ↓
进入进程监管循环
```

### 4.2 高级多容器模式

下列显式命令继续受支持：

| 职责 | 命令 |
|---|---|
| 数据库迁移 | `alembic upgrade head` |
| 旧任务对账 | `python -m app.reconcile` |
| API | `uvicorn app.main:app ...` |
| Scheduler | `python -m app.scheduler` |
| Worker | `python -m app.worker` |
| Web 前端 | `nginx -g 'daemon off;'` |

高级 Compose 可以继续让每个容器只运行一个主进程。该模式用于开发、故障隔离、
架构验证和未来更复杂的部署方案，不作为普通 NAS 用户的默认文档入口。

两种模式必须使用相同的应用代码、数据库契约和 Task Engine，不得形成两套业务
实现。

## 5. 进程和职责不变量

单容器只合并部署生命周期，不合并业务职责：

```text
API
├── CRUD
├── 查询状态
└── 创建手动任务

Scheduler
└── 发现到期订阅并创建任务

Worker
└── 领取并执行任务

Launcher
├── 启动屏障
├── 启停子进程
├── 信号转发
└── 汇总进程健康
```

必须始终满足：

- API 不执行扫描和转存；
- Scheduler 不领取或执行任务；
- Worker 是唯一任务执行者；
- Launcher 不导入或调用 Task Handler、Provider 或业务 Service；
- SQLite 部署只有一个 Scheduler 和一个 Worker；
- Worker 并发度保持为 1；
- 子进程共享容器和 `/data`，但不共享隐式内存状态；
- 所有跨职责协作继续通过 SQLite 持久化状态完成。

Launcher 是进程生命周期边界，不是新的业务层。

## 6. Launcher 契约

### 6.1 实现约束

Launcher 应使用项目内置的 Python 模块实现，避免为了四个受控子进程引入
systemd、Docker-in-Docker 或体积较大的第三方进程管理系统。

Launcher 必须：

- 作为容器 PID 1 运行；
- 使用明确的命令列表启动子进程，不通过 Shell 拼接用户输入；
- 回收所有退出的子进程，避免僵尸进程；
- 把 `SIGTERM`、`SIGINT` 等停止信号转发给子进程；
- 给 Worker 保留不短于现有 `stop_grace_period` 的优雅退出时间；
- 任一关键常驻进程意外退出时停止其余进程并以非零状态退出；
- 不在容器内部无限重启单个子进程；
- 不吞掉子进程退出码或标准输出。

不在容器内无限重启单个进程，是为了避免出现 Nginx、API、Scheduler 或 Worker
长期失效，但容器仍显示正常运行的半失效状态。恢复责任交给容器重启策略。

### 6.2 启动顺序

1. 验证 `/data` 可写；
2. 加载或生成持久化运行时配置；
3. 执行 Alembic 迁移，失败立即退出；
4. 执行旧任务对账，失败立即退出；
5. 启动 API；
6. 在有界时间内等待 API 健康，超时立即退出；
7. 启动 Nginx、Scheduler 和 Worker；
8. 进入进程监管循环。

迁移和对账不得与 Worker 并发运行。

### 6.3 停止顺序

收到停止信号后：

1. 停止 Nginx，拒绝新的 Web 请求；
2. 停止 Scheduler，避免创建新任务；
3. 通知 Worker 优雅停止并等待当前安全边界；
4. 停止 API；
5. 超过总宽限时间后强制结束剩余子进程；
6. Launcher 退出。

若 Worker 在远端操作期间被强制终止，后续启动仍依赖已有租约恢复、fencing 和
远端对账，不得在 Launcher 中另写一套恢复逻辑。

## 7. 持久化目录契约

Appliance 模式只有一个必须挂载的目录：

```text
/data
├── mediasync.db
├── mediasync.db-wal
├── mediasync.db-shm
├── config/
│   └── runtime-secrets.json
└── logs/                  # 仅保留确有必要的持久化日志
```

数据库、运行时密钥和未来可持久化配置都必须位于 `/data`。镜像升级和容器重建
不得依赖容器可写层中的文件。

默认安装文档必须优先使用宿主机目录绑定：

```text
宿主机目录 → /data
```

不得要求普通用户理解 Docker named volume 才能完成备份。

## 8. 首次启动和密钥

### 8.1 配置优先级

运行时配置按以下优先级解析：

1. 显式环境变量；
2. `/data/config/runtime-secrets.json` 中已持久化的值；
3. 首次启动时生成的值；
4. 非敏感配置的应用默认值。

`SECRET_KEY` 和 `CREDENTIAL_ENCRYPTION_KEY` 一旦写入持久化文件，后续环境
变量与其不一致时必须拒绝启动并给出明确错误。rc.3 不提供隐式加密密钥轮换。

`ADMIN_PASSWORD` 不属于数据解密密钥。用户后续显式提供新的
`ADMIN_PASSWORD` 时，Launcher 可以原子更新持久化管理员密码，用作 NAS 环境下
的离线密码重置；不得在日志中输出新值。

### 8.2 自动生成

首次启动且没有外部配置时，Launcher 生成：

- `SECRET_KEY`；
- `CREDENTIAL_ENCRYPTION_KEY`；
- 初始管理员密码。

要求：

- 使用密码学安全随机源；
- 先写临时文件并 `fsync`，再原子替换目标文件；
- 配置目录权限为 `0700`，文件权限为 `0600`；
- 日志不得输出 `SECRET_KEY` 或 `CREDENTIAL_ENCRYPTION_KEY`；
- 自动生成的管理员密码只在首次创建时输出一次；
- 后续重启不得重复输出管理员密码。

用户显式提供 `ADMIN_PASSWORD` 时，不在日志中输出该值。首次启动后遗失自动
生成的管理员密码时，用户可以通过显式设置 `ADMIN_PASSWORD` 并重启容器完成
离线重置。

### 8.3 丢失保护

如果数据库已经存在，但持久化密钥文件缺失，并且环境变量没有提供完整的原有
密钥，Launcher 必须拒绝启动。

不得静默生成新的 `CREDENTIAL_ENCRYPTION_KEY`，否则数据库中的云盘凭证将变成
不可解密数据。

备份说明必须把以下内容视为一个不可分割的恢复单元：

```text
/data/mediasync.db*
/data/config/runtime-secrets.json
```

## 9. Web、端口和会话安全

默认容器只暴露 Nginx HTTP 端口 `80`：

```text
宿主机 8080 → 容器 80
```

API 端口 `8000` 只供容器内部 Nginx 使用，不应在普通安装示例中映射。

当前 `ENVIRONMENT=production` 会让会话 Cookie 无条件带 `Secure`，导致局域网
HTTP 安装可能出现登录循环。rc.3 必须把 Cookie 安全属性从环境名称中拆出，
提供明确配置：

```text
SESSION_COOKIE_SECURE=false   # 默认局域网 HTTP
SESSION_COOKIE_SECURE=true    # HTTPS 反向代理
```

安全要求：

- 默认文档明确说明不得把 HTTP 管理端口直接暴露到公网；
- 公网或跨互联网访问必须使用 HTTPS，并设置 `SESSION_COOKIE_SECURE=true`；
- Nginx 必须继续设置必要的代理头；
- Appliance 模式不得挂载 Docker Socket、使用特权模式或扩大 Linux capabilities。

## 10. 健康检查和可观测性

容器健康不能只检查首页。健康检查必须同时确认：

- Launcher 正在运行；
- Nginx 正在运行并能响应；
- API 健康端点可访问；
- Scheduler 子进程仍存活；
- Worker 子进程仍存活。

Launcher 应提供仅限容器本地访问的状态接口，例如 Unix Socket。健康检查客户端
通过该接口获取 Launcher 当前持有的子进程状态，避免依赖可能过期的 PID 文件。

健康结果至少区分：

```json
{
  "launcher": true,
  "nginx": true,
  "api": true,
  "scheduler": true,
  "worker": true
}
```

所有子进程继续写标准输出和标准错误。Launcher 可以给日志增加来源前缀，但不得
改写、截断或只写入容器内部文件。

## 11. 故障和恢复语义

| 场景 | 必须行为 |
|---|---|
| `/data` 不可写 | 启动失败，指出目录和权限问题 |
| 密钥文件首次写入失败 | 启动失败，不启动业务进程 |
| 已有数据库但密钥丢失 | 启动失败，不生成替代密钥 |
| Alembic 迁移失败 | 启动失败，不运行对账、API 或 Worker |
| 旧任务对账失败 | 启动失败，不运行常驻进程 |
| API 未在时限内健康 | 停止全部已启动进程并退出 |
| Nginx 意外退出 | 停止其余子进程并非零退出 |
| Scheduler 意外退出 | 停止其余子进程并非零退出 |
| Worker 意外退出 | 停止其余子进程并非零退出 |
| NAS 在任务期间重启 | 启动后通过租约恢复和对账继续处理 |
| 正常停止超时 | 强制结束，下一次启动执行恢复 |

单容器模式不能保证某个子进程崩溃时其他服务继续可用。该成本由更简单的 NAS
部署换取，并通过快速失败、Docker 重启和持久化任务恢复降低影响。

## 12. 兼容性、升级和回滚

### 12.1 rc.2 升级到 rc.3

升级步骤：

1. 停止 rc.2 Compose 服务；
2. 备份 rc.2 SQLite 数据库和现有密钥环境变量；
3. 准备宿主机 `/data` 目录；
4. 把数据库文件复制到 `/data`；
5. 首次启动 rc.3 时提供 rc.2 使用的 `SECRET_KEY`、
   `CREDENTIAL_ENCRYPTION_KEY` 和管理员密码；
6. rc.3 把敏感配置持久化后，验证账号凭证可解密；
7. 验证 API、Scheduler、Worker 和前端健康；
8. 完成后停用旧 Compose 项目。

不得同时运行 rc.2 多容器部署和 rc.3 Appliance 容器访问同一个 SQLite 数据库。

### 12.2 回滚

回滚到 rc.2：

1. 停止 rc.3 Appliance 容器；
2. 备份完整 `/data`；
3. 在 rc.2 Compose 中恢复相同数据库和密钥；
4. 使用精确镜像标签 `v0.2.0-rc.2`；
5. 启动迁移、对账和常驻服务；
6. 验证云盘账号解密和任务恢复。

rc.3 不应引入数据库迁移；若实现阶段发现必须修改数据库，必须另开设计 PR。

## 13. 验收标准

运行时实现合并前必须验证：

- 在没有 Compose 和源码的环境中，仅用 `docker run` 成功启动；
- 只映射端口和空 `/data` 目录时能完成首次启动；
- 自动生成的密钥在容器重建后保持不变；
- 自动生成的管理员密码只输出一次；
- 已有数据库但缺少原密钥时拒绝启动；
- 迁移失败或对账失败时 Worker 永远不会启动；
- API、Scheduler、Worker 仍然是独立进程；
- API 不执行任务，Scheduler 不执行任务，Worker 是唯一执行者；
- 任一关键子进程退出会让容器非零退出；
- `SIGTERM` 能按约定顺序停止进程；
- Worker 被强制终止后，任务通过现有租约和对账恢复；
- 健康检查能识别 Nginx、API、Scheduler、Worker 任一失效；
- 局域网 HTTP 模式可以登录；
- HTTPS 模式使用 `Secure` Cookie；
- 高级多容器 Compose 仍可运行；
- SQLite 仍然只有一个 Scheduler 和一个 Worker；
- Docker Hub 和 GHCR 的精确版本支持 `amd64`、`arm64`；
- 镜像不需要特权模式或 Docker Socket。

必须至少在飞牛式图形化 Docker 环境完成一次真实安装验收：

```text
搜索镜像 → 下载 → 创建容器 → 映射端口 → 映射 /data → 启动
```

## 14. 实现拆分

设计 PR 合并后创建一个实现 Issue，但运行时代码按可审查的 PR 拆分：

1. **持久化启动配置**
   - `/data` 契约；
   - 密钥生成、加载和丢失保护；
   - 数据层与配置测试。
2. **Appliance Launcher**
   - 启动屏障；
   - 子进程监管；
   - 信号与退出码；
   - 故障注入测试。
3. **镜像入口与健康检查**
   - 默认 CMD；
   - 本地健康接口；
   - 单容器镜像测试；
   - 保留高级显式命令。
4. **用户部署体验**
   - Docker Hub Overview；
   - 飞牛部署文档；
   - `docker run` 示例；
   - rc.2 升级与回滚演练。
5. **Release PR**
   - 版本与变更日志；
   - 多架构发布；
   - Docker Hub、GHCR 匿名验证；
   - 发布 v0.2.0-rc.3。

每个运行时 PR 必须引用本设计、ADR-0004 和实现 Issue，不得顺带修改 Provider、
Task Engine、UI 功能或数据库模型。
