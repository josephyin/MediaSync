# MediaSync MVP 设计方案

> 第一阶段仅支持 Aliyun Drive。本方案以四张核心业务表为骨架，同时保留 Provider 扩展、凭证安全、增量检测、幂等转存、失败重试和任务追踪能力。

## 1. 产品边界

MediaSync 第一阶段只负责“资源分享 -> 个人云盘”：

```text
资源分享
   ↓
MediaSync（监控、增量检测、转存）
   ↓
个人云盘
   ↓
OpenList / SmartStrm / MoviePilot
   ↓
Emby / Jellyfin / 飞牛影视
```

STRM 生成、媒体刮削、重命名、媒体库整理和播放不属于 MVP。

第一版采用单实例、单管理员模式，数据库只建立以下四张核心业务表：

- `cloud_accounts`：云盘账号
- `subscriptions`：分享订阅
- `files`：增量文件索引及转存结果
- `tasks`：扫描、转存、凭证刷新任务及运行历史

后台管理员账号通过环境变量或 Docker Secret 配置，暂不建立用户表。后续需要多用户时再增加 `users` 和 `user_sessions`，不影响四张业务表的主体结构。

## 2. 项目目录

```text
MediaSync/
├── backend/
│   ├── app/
│   │   ├── main.py                     # FastAPI 入口及生命周期
│   │   ├── api/
│   │   │   ├── deps.py                 # 数据库、认证、分页依赖
│   │   │   └── v1/
│   │   │       ├── router.py
│   │   │       ├── auth.py
│   │   │       ├── cloud_accounts.py
│   │   │       ├── subscriptions.py
│   │   │       ├── files.py
│   │   │       ├── tasks.py
│   │   │       └── system.py
│   │   ├── core/
│   │   │   ├── config.py
│   │   │   ├── database.py
│   │   │   ├── security.py             # 管理员认证、凭证加解密
│   │   │   ├── logging.py
│   │   │   └── exceptions.py
│   │   ├── models/
│   │   │   ├── base.py
│   │   │   ├── cloud_account.py
│   │   │   ├── subscription.py
│   │   │   ├── file.py
│   │   │   └── task.py
│   │   ├── schemas/                     # Pydantic 请求/响应模型
│   │   ├── repositories/                # SQLAlchemy 数据访问
│   │   ├── services/
│   │   │   ├── account_service.py
│   │   │   ├── subscription_service.py
│   │   │   ├── scan_service.py
│   │   │   └── transfer_service.py
│   │   ├── providers/
│   │   │   ├── base.py                  # Provider 协议及通用 DTO
│   │   │   ├── registry.py
│   │   │   └── aliyundrive/
│   │   │       ├── provider.py
│   │   │       ├── client.py
│   │   │       ├── schemas.py
│   │   │       └── exceptions.py
│   │   ├── scheduler/
│   │   │   ├── manager.py
│   │   │   └── jobs.py
│   │   └── utils/
│   ├── migrations/                      # Alembic 迁移
│   ├── tests/
│   │   ├── unit/
│   │   ├── integration/
│   │   └── fixtures/
│   ├── alembic.ini
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── api/
│   │   ├── components/
│   │   ├── layouts/
│   │   ├── router/
│   │   ├── stores/
│   │   ├── types/
│   │   └── views/
│   │       ├── LoginView.vue
│   │       ├── DashboardView.vue
│   │       ├── AccountsView.vue
│   │       ├── SubscriptionsView.vue
│   │       ├── FilesView.vue
│   │       └── TasksView.vue
│   ├── package.json
│   ├── vite.config.ts
│   └── Dockerfile
├── docs/
│   ├── mvp-design.md
│   └── provider-development.md
├── deploy/nginx.conf
├── data/                                 # 数据库和日志挂载目录
├── .env.example
├── .gitignore
├── docker-compose.yml
├── LICENSE
├── CONTRIBUTING.md
└── README.md
```

API 层只处理 HTTP 契约，Service 负责编排业务，Repository 访问数据库，Provider 封装不同云盘接口。路由、调度任务和业务服务不能直接依赖阿里云盘响应结构。

## 3. Provider 设计

第一版只有 Aliyun Drive，但业务层从一开始依赖统一接口：

```python
class CloudDriveProvider(Protocol):
    async def validate_account(self) -> AccountProfile: ...
    async def refresh_credentials(self) -> CredentialUpdate: ...
    async def resolve_share(self, share_url: str) -> ShareInfo: ...
    async def list_share_items(self, share: ShareRef, parent_id: str) -> Page[RemoteItem]: ...
    async def resolve_target_path(self, path: str) -> FolderRef: ...
    async def ensure_folder(self, parent: FolderRef, name: str) -> FolderRef: ...
    async def save_shared_item(self, source: ShareItemRef, target: FolderRef) -> SaveResult: ...
    async def find_target_item(self, target: FolderRef, name: str) -> RemoteItem | None: ...
```

Provider 注册表按 `provider` 字段创建实现：

```text
aliyundrive -> AliyunDriveProvider
quark       -> QuarkDriveProvider       # 后续
115         -> Drive115Provider         # 后续
onedrive    -> OneDriveProvider         # 后续
```

通用远端文件 DTO 至少包含 `remote_file_id`、`parent_id`、`filename`、`item_type`、`size`、`content_hash` 和 `updated_at`。云盘特有字段放入 `metadata`，不能渗透到通用扫描流程。

## 4. 数据库模型

### 4.1 通用约定

- SQLite 开启 foreign keys 和 WAL。
- 时间统一以 UTC 保存，API 使用 ISO 8601 返回。
- 主键使用整数自增。
- 所有凭证和分享密码加密保存，日志及 API 响应不得回显。
- 状态值在 Python 中使用枚举，在数据库中保存稳定的小写字符串。
- 唯一约束必须落在数据库中，不能只依赖应用层查询。

### 4.2 `cloud_accounts`

保留“云盘账号”的简洁结构，同时补足安全校验和 Provider 运行需要的信息。

| 字段 | 类型 | 约束/说明 |
|---|---|---|
| id | integer | PK |
| provider | varchar(32) | NOT NULL，MVP 为 `aliyundrive` |
| name | varchar(100) | NOT NULL，用户自定义名称 |
| refresh_token | text | NOT NULL，存储加密后的 token |
| account_identity | varchar(255) | nullable，脱敏账号昵称/标识 |
| provider_user_id | varchar(128) | nullable，私有/Open 同账号校验依据 |
| default_drive_id | varchar(128) | nullable，Provider 目标 drive |
| status | varchar(20) | active / expired / error / disabled |
| last_verified_at | datetime | nullable |
| last_error | text | nullable，必须脱敏 |
| open_auth_mode | varchar(20) | nullable，alistgo/openlist/custom |
| open_refresh_token | text | nullable，加密后的 Open token |
| open_client_id | varchar(255) | nullable，自有应用 Client ID |
| open_client_secret | text | nullable，加密后的 Client Secret |
| open_token_url | text | nullable，AListGo 模式 HTTPS Token URL |
| open_account_identity | varchar(255) | nullable，OpenAPI 账号标识 |
| open_status | varchar(20) | nullable，pending/active/error |
| open_last_verified_at | datetime | nullable |
| open_last_error | text | nullable，必须脱敏 |
| created_at | datetime | NOT NULL |
| updated_at | datetime | NOT NULL |

约束：`UNIQUE(provider, name)`。

字段名使用 `refresh_token` 保持 API 和模型直观，但数据库中的字段内容必须先使用 `CREDENTIAL_ENCRYPTION_KEY` 加密。查询账号的 API 永远不返回该字段。

### 4.3 `subscriptions`

保留订阅的名称、分享地址、目标路径、周期和开关，同时加入账号关联、扫描状态和稳定远端标识。

| 字段 | 类型 | 约束/说明 |
|---|---|---|
| id | integer | PK |
| cloud_account_id | integer | FK cloud_accounts, indexed |
| name | varchar(150) | NOT NULL |
| provider | varchar(32) | NOT NULL |
| share_url | text | NOT NULL |
| share_key | varchar(255) | nullable，Provider 解析后的稳定分享 ID |
| share_password | text | nullable，加密保存 |
| source_folder_id | varchar(255) | nullable，默认分享根目录 |
| target_path | text | NOT NULL，用户可读目标路径 |
| target_drive_id | varchar(128) | nullable，订阅实际写入的 Drive ID |
| target_drive_type | varchar(20) | nullable，default/resource/backup/custom |
| target_folder_id | varchar(255) | nullable，解析后的稳定目标目录 ID |
| schedule | varchar(100) | NOT NULL，例如 `interval:30m` |
| enabled | boolean | NOT NULL, default true |
| status | varchar(20) | pending / active / scanning / error / disabled |
| initial_sync_mode | varchar(20) | all / future_only，默认 all |
| last_scanned_at | datetime | nullable |
| next_scan_at | datetime | nullable |
| last_error | text | nullable，必须脱敏 |
| created_at | datetime | NOT NULL |
| updated_at | datetime | NOT NULL |

约束：

- `cloud_account_id` 必须对应相同 `provider` 的账号。
- 建议唯一索引：`(cloud_account_id, share_key, source_folder_id, target_folder_id)`。
- `schedule` 只接受系统定义的 interval/cron 格式，不能接受可执行表达式。

同时保存 `target_drive_id`、`target_path` 和 `target_folder_id`：盘 ID 决定写入默认盘、资源库还是备份盘；路径用于界面展示；目录 ID 用于稳定调用云盘 API。目录被移动或重命名后，可重新解析并更新展示路径。

### 4.4 `files`

`files` 既是分享目录的增量索引，也是用户可见的文件转存记录。

| 字段 | 类型 | 约束/说明 |
|---|---|---|
| id | integer | PK |
| subscription_id | integer | FK subscriptions, indexed |
| remote_file_id | varchar(255) | NOT NULL，分享侧稳定文件 ID |
| parent_remote_file_id | varchar(255) | nullable |
| filename | varchar(255) | NOT NULL |
| relative_path | text | NOT NULL，订阅根目录下的相对路径 |
| item_type | varchar(16) | file / folder |
| size | bigint | nullable |
| content_hash | varchar(255) | nullable |
| fingerprint | varchar(255) | NOT NULL，版本指纹 |
| status | varchar(20) | discovered / pending / saving / saved / failed / skipped |
| target_file_id | varchar(255) | nullable，转存成功后的目标文件 ID |
| target_path | text | nullable，实际目标路径快照 |
| first_seen_at | datetime | NOT NULL |
| last_seen_at | datetime | NOT NULL |
| saved_at | datetime | nullable |
| last_error | text | nullable，必须脱敏 |
| created_at | datetime | NOT NULL |
| updated_at | datetime | NOT NULL |

核心约束：`UNIQUE(subscription_id, remote_file_id)`。

增量判断优先使用 Provider 的稳定 `remote_file_id`。`fingerprint` 由文件 ID、类型、大小、hash、远端更新时间等归一化生成，用来判断相同 ID 下的内容是否发生变化。MVP 不同步分享端删除操作，也不会删除用户云盘文件。

### 4.5 `tasks`

`tasks` 同时承担轻量任务队列、转存历史和后台日志索引，避免 MVP 过早拆出多张运行表。

| 字段 | 类型 | 约束/说明 |
|---|---|---|
| id | integer | PK |
| subscription_id | integer | nullable, FK subscriptions, indexed |
| file_id | integer | nullable, FK files, indexed |
| type | varchar(32) | scan / transfer / refresh_token |
| trigger_type | varchar(20) | scheduled / manual / retry |
| status | varchar(20) | pending / running / success / failed / skipped / canceled |
| idempotency_key | varchar(255) | nullable, UNIQUE |
| message | text | nullable，执行摘要或错误信息，必须脱敏 |
| error_code | varchar(100) | nullable |
| attempt_count | integer | NOT NULL, default 0 |
| max_attempts | integer | NOT NULL, default 3 |
| created_at | datetime | NOT NULL |
| started_at | datetime | nullable |
| finished_at | datetime | nullable |
| updated_at | datetime | NOT NULL |

规则：

- scan task 的 `file_id` 为空，transfer task 必须关联 `file_id`。
- 转存任务幂等键建议为 `transfer:{subscription_id}:{remote_file_id}:{fingerprint}`。
- 自动重试更新同一条 task 和 `attempt_count`，不生成重复历史。
- 扫描开始前检查同一订阅是否已有 running scan task。

### 4.6 表关系

```text
cloud_accounts 1 ── N subscriptions
subscriptions  1 ── N files
subscriptions  1 ── N tasks
files          1 ── N tasks
```

这种设计保留四张表的简洁性，同时满足：

- 多云盘账号选择
- Provider 插件路由
- 分享目录增量检测
- 文件版本变化识别
- 幂等转存和失败重试
- 转存历史及日志查看

## 5. 核心流程

### 5.1 扫描流程

1. APScheduler 根据 `subscriptions.schedule` 触发扫描。
2. 创建 scan task，并防止同一订阅并发扫描。
3. 根据 `cloud_account_id` 加载账号，由 Provider 校验或刷新凭证。
4. 解析分享链接和来源目录，递归分页获取文件。
5. 按 `(subscription_id, remote_file_id)` upsert `files`，更新 `last_seen_at`。
6. 新文件根据 `initial_sync_mode` 决定是否创建 transfer task。
7. 更新订阅扫描时间、下次执行时间、状态和 task 摘要。

`all`：首次扫描为已有文件创建转存任务。

`future_only`：首次扫描只建立文件基线，后续新增文件才创建转存任务。

### 5.2 转存流程

1. Worker 批量领取 pending transfer tasks。
2. 把 task 和 file 状态更新为 running/saving。
3. 按 `relative_path` 在目标盘创建缺失目录。
4. 转存前通过 `target_folder_id + filename` 或 Provider 幂等能力检查目标。
5. 调用 Provider 转存分享文件。
6. 成功后写入 `target_file_id`、`target_path`、`saved_at`。
7. 失败时记录脱敏错误；未达到 `max_attempts` 时按退避策略重试。

目标检查用于处理“云盘已经转存成功，但本地数据库尚未提交时进程退出”的场景，避免重启后生成重复文件。

## 6. API 设计

基础路径为 `/api/v1`，JSON 字段统一使用 `snake_case`。

分页响应：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0
}
```

错误响应：

```json
{
  "error": {
    "code": "SUBSCRIPTION_NOT_FOUND",
    "message": "Subscription not found",
    "request_id": "...",
    "details": null
  }
}
```

### 6.1 管理员认证

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/auth/login` | 管理员登录，设置签名 HttpOnly Cookie |
| POST | `/auth/logout` | 清除登录状态 |
| GET | `/auth/status` | 查询登录状态 |

第一版不开放注册。部署到公网时必须配置 HTTPS，管理员密码哈希通过环境变量或 Docker Secret 提供。

### 6.2 云盘账号

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/provider-types` | 查询已安装 Provider 及能力 |
| GET | `/cloud-accounts` | 查询云盘账号 |
| POST | `/cloud-accounts` | 添加云盘账号 |
| GET | `/cloud-accounts/{id}` | 账号详情，不返回 refresh token |
| PATCH | `/cloud-accounts/{id}` | 修改名称、凭证或状态 |
| DELETE | `/cloud-accounts/{id}` | 删除未被订阅引用的账号 |
| POST | `/cloud-accounts/{id}/verify` | 校验账号并刷新状态 |
| PUT | `/cloud-accounts/{id}/open-credential` | 配置 AListGo 或自有应用 Open 凭证 |
| POST | `/cloud-accounts/{id}/open-credential/verify` | 校验 Open 凭证及同账号关系 |
| DELETE | `/cloud-accounts/{id}/open-credential` | 解绑并清除 Open 凭证 |
| GET | `/cloud-accounts/{id}/drives` | 查询可用 drive |
| GET | `/cloud-accounts/{id}/folders` | 浏览目标目录 |

添加账号：

```json
{
  "provider": "aliyundrive",
  "name": "我的阿里云盘",
  "refresh_token": "user-input-secret"
}
```

### 6.3 分享和订阅

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/shares/resolve` | 解析分享链接 |
| POST | `/shares/browse` | 浏览分享目录 |
| GET | `/subscriptions` | 查询订阅 |
| POST | `/subscriptions` | 新建订阅 |
| GET | `/subscriptions/{id}` | 订阅详情 |
| PATCH | `/subscriptions/{id}` | 修改订阅 |
| DELETE | `/subscriptions/{id}` | 删除订阅，不删除目标盘文件 |
| POST | `/subscriptions/{id}/scan` | 手动触发扫描，返回 task |
| GET | `/subscriptions/{id}/files` | 查询订阅文件 |
| GET | `/subscriptions/{id}/tasks` | 查询订阅任务 |

创建订阅：

```json
{
  "name": "某剧更新",
  "cloud_account_id": 1,
  "provider": "aliyundrive",
  "share_url": "https://www.alipan.com/s/example",
  "share_password": null,
  "source_folder_id": "root",
  "target_path": "/Media/电视剧/某剧",
  "schedule": "interval:30m",
  "initial_sync_mode": "all",
  "enabled": true
}
```

服务端必须重新解析分享并校验来源目录、账号 Provider 和目标目录，不能信任前端传入的展示信息。

### 6.4 文件与转存历史

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/files` | 按订阅、状态、名称、时间查询文件 |
| GET | `/files/{id}` | 文件详情和关联任务 |
| POST | `/files/{id}/retry` | 重试失败文件 |
| POST | `/files/retry-failed` | 按筛选条件批量重试 |

### 6.5 任务、日志和系统

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/tasks` | 查询任务，支持类型、状态和时间筛选 |
| GET | `/tasks/{id}` | 任务详情 |
| GET | `/admin/logs` | 查询脱敏后的应用日志 |
| GET | `/dashboard/summary` | 仪表盘统计 |
| GET | `/system/health` | 存活检查 |
| GET | `/system/info` | 版本、Provider 和调度状态 |

手动扫描和重试接口返回 `202 Accepted`，表示任务已经接受，不表示远端转存已经完成。

## 7. 调度和部署约束

- 使用 `AsyncIOScheduler`，在 FastAPI lifespan 中启动和关闭。
- 应用启动时读取启用的 subscriptions 并重建调度 Job。
- 订阅增删改时同步更新 APScheduler Job。
- transfer worker 使用单独的周期 Job 领取 pending tasks，并限制并发。
- 处理阿里云盘限流、凭证过期、网络超时和指数退避。
- SQLite 部署只运行一个 Uvicorn worker，避免重复启动调度器。
- Docker Compose 包含 backend 和 frontend，数据目录挂载到 `/data`。
- SQLite 位于 `/data/mediasync.db`，日志位于 `/data/logs`。
- `SECRET_KEY`、`CREDENTIAL_ENCRYPTION_KEY` 和管理员密码通过环境变量或 Secret 传入。

后续需要多副本时，应迁移 PostgreSQL 和分布式任务队列/锁，不继续依赖 SQLite + 进程内调度器。

## 8. 前端页面

1. 登录：单管理员登录。
2. 仪表盘：订阅、待转存、成功、失败和最近任务统计。
3. 云盘账号：添加、校验、更新 token、启停和目录浏览。
4. 分享订阅：解析分享、选择来源和目标目录、设置周期及首次同步策略。
5. 文件记录：查询增量文件、转存状态、保存时间和失败原因。
6. 任务日志：查询扫描、转存、刷新任务并执行重试。

## 9. 开发计划

| 阶段 | 主要交付 | 预计工时 |
|---|---|---:|
| 0. Aliyun Drive 技术验证 | 登录刷新、分享解析、分页遍历、目录创建、转存、限流验证 | 2-4 人日 |
| 1. 工程骨架 | 后端/前端、配置、日志、异常、Alembic、Docker Compose、CI | 2-3 人日 |
| 2. 认证与云盘账号 | 单管理员认证、凭证加密、账号 CRUD、校验和目录浏览 | 3-5 人日 |
| 3. Provider 与订阅 | Provider 抽象、Aliyun 实现、分享解析、订阅 CRUD | 4-6 人日 |
| 4. 同步核心 | 递归扫描、增量索引、路径映射、幂等转存和失败重试 | 6-9 人日 |
| 5. 调度与可观测 | APScheduler、task worker、历史、日志、健康检查 | 3-4 人日 |
| 6. Web 管理后台 | 登录、账号、订阅、文件、任务、仪表盘页面及联调 | 5-7 人日 |
| 7. 测试与开源发布 | 单元/集成测试、镜像、README、贡献指南和发布流程 | 4-6 人日 |
| **合计** | 可公开试用的 MVP | **29-44 人日** |

如果 Aliyun Drive 只能依赖非官方协议或复杂登录流程，额外预留 5-10 人日及持续维护成本。阶段 0 是开发门槛，应先确认账号、分享和转存链路可稳定调用。

建议里程碑：

- M1：完成账号校验、分享解析、目录遍历和单文件转存技术闭环。
- M2：完成订阅、首次全量/未来新增、定时扫描、增量和幂等重试。
- M3：完成 Web 后台、测试、Docker、README 和 `v0.1.0` 发布。

## 10. 验收标准

- 同一分享文件重复扫描 10 次，只产生一个文件记录和一个目标文件。
- `all` 首次扫描会转存已有文件；`future_only` 首次扫描只建立基线。
- 支持嵌套目录、分页、空目录、同名文件和超大目录。
- 支持分享失效、密码错误、账号过期、限流和网络超时。
- 云盘转存成功但进程在本地提交前退出，重启后不会重复转存。
- 禁用订阅后不再扫描，手动与定时扫描不会并发执行。
- API 和日志不出现明文 refresh token、分享密码或 Cookie。
- 容器重启后数据库、订阅调度和待重试任务能够恢复。
- README 能让用户仅通过 Docker Compose 完成配置、启动和首次订阅。

## 11. MVP 暂不实现

- STRM 生成、媒体刮削、重命名和播放器管理。
- 分享端删除后同步删除个人云盘内容。
- 文件双向同步和个人云盘反向扫描。
- Quark Drive、115、OneDrive 等其他 Provider。
- 多用户、复杂权限、多节点和高可用。
- WebSocket 实时日志，第一版使用分页或轮询。
