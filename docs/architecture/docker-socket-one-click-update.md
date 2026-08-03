# Docker Socket 一键镜像升级设计

- 状态：已接受
- 日期：2026-07-31
- 目标版本：v0.3
- 相关决策：[ADR-0005：Docker Socket 可选一键镜像升级](adr/ADR-0005-optional-docker-socket-updater.md)
- 取代：无

## 1. 背景

MediaSync 默认单容器 Appliance 已把普通 NAS 用户的安装流程简化为：

```text
下载一个镜像
    ↓
映射 9090 端口
    ↓
映射 /data
    ↓
启动容器
```

当前升级仍要求用户在群晖、飞牛或命令行中下载新镜像，再保留原 `/data` 重建
容器。这个过程对熟悉 Docker 的用户很直接，但普通用户容易遗漏数据目录、端口、
环境变量或重启策略。

Docker 容器不能仅靠自身进程替换正在运行的容器。完整镜像升级必须通过 Docker
Engine API 完成，而 Unix Socket `/var/run/docker.sock` 是最普遍的本地 Docker
Engine 控制入口。

挂载 Docker Socket 会让 MediaSync 获得接近宿主机 Docker 管理员的权限。这是
安全模型和默认部署能力的变化，不能作为普通 UI 功能直接加入运行时代码。

## 2. 决策摘要

MediaSync 可以提供基于 Docker Socket 的一键镜像升级，但必须满足：

- 默认关闭，不改变现有用户的容器权限；
- 只有用户显式挂载 Docker Socket 后才启用；
- 普通运行期间仍只有一个 MediaSync 容器；
- 升级切换期间创建一个短生命周期 updater 助手容器；
- updater 完成切换或回滚后必须自动删除；
- 只允许管理当前 MediaSync 容器，不提供通用 Docker 管理能力；
- 更新目标必须是官方镜像的精确版本和精确 OCI digest；
- 更新前必须停止任务领取并创建一致性快照；
- 新版本未通过健康验证前，Worker 不得执行任务；
- 失败时必须恢复旧镜像和更新前数据；
- 无 Docker Socket 时仍提供版本检查和人工升级说明。

本设计不在容器内下载并覆盖 Python、前端或系统文件。MediaSync 的版本事实来源
继续是不可变 OCI 镜像。

## 3. 目标

- 管理员可以在 Web 后台检查新版本；
- 管理员可以查看版本号、发布时间、更新说明、镜像 digest 和升级要求；
- 支持稳定版和 RC 两个更新频道；
- 支持从官方 Docker Hub 或 GHCR 拉取目标镜像；
- 支持从 Web 后台发起完整镜像升级；
- 自动保留端口、环境变量、数据挂载、网络和重启策略；
- 自动完成数据库一致性快照、容器切换、健康验证和失败回滚；
- 更新过程有持久化状态，页面恢复后可以查看最终结果；
- 不要求常驻 Watchtower 或另一个 updater 应用。

## 4. 非目标

首个实现不支持：

- 定时、静默或无人确认的自动安装；
- 管理、重启或升级其他 Docker 容器；
- 修改 NAS 防火墙、共享目录、反向代理或证书；
- Docker TCP API、远程 Docker 主机或 Kubernetes；
- 高级多容器 Compose 部署的一键重建；
- 用户自定义镜像仓库、私有 Registry 或任意镜像地址；
- 跨大版本自动迁移；
- 自动降级到任意历史版本；
- 绕过 Release Note 中声明的人工迁移要求；
- 把 Docker Socket 作为默认镜像声明的必需挂载。

高级 Compose 继续使用：

```bash
docker compose pull
docker compose up -d --force-recreate
```

## 5. 核心不变量

### 5.1 权限必须显式启用

- 官方镜像不得默认挂载 Docker Socket。
- 无 Socket 时，应用必须正常启动并保留全部同步功能。
- 只有以下探测全部成功时，后端才报告支持一键升级：
  - Socket 是 Unix Socket；
  - Docker Engine API 可访问；
  - 可以唯一识别当前容器；
  - 当前容器是默认单容器 Appliance；
  - `/data` 是持久化挂载；
  - 当前部署不带 Compose 管理标签。
- 探测失败只能关闭安装能力，不得影响 API、Scheduler 或 Worker。
- 前端必须明确提示 Docker Socket 等同 Docker 管理员权限。

推荐的可选挂载为：

```text
/var/run/docker.sock:/var/run/docker.sock
```

不得声称 `:ro` 能限制 Docker API 为只读。Socket 文件系统挂载为只读时，客户端
仍可能通过 Socket 发送具有写入效果的 Docker API 请求。

### 5.2 只管理自身

Updater 不得接受任意容器 ID、镜像名、命令、挂载路径或 Docker API 路径。

当前容器按以下顺序识别：

1. 显式配置的不可变容器标识；
2. 默认 Docker `HOSTNAME` 对应的当前容器 ID；
3. 官方 OCI 标签、`/data` 挂载和进程模式共同得到的唯一匹配。

如果得到零个或多个候选容器，必须拒绝一键升级。不得选择“第一个匹配容器”。

升级前必须再次验证：

- 容器镜像带有 MediaSync 官方来源标签；
- 容器入口是 Appliance 模式；
- 目标数据挂载在容器内的路径精确为 `/data`；
- 当前容器没有 Compose 管理标签；
- 当前容器 ID 与创建升级操作时记录的 ID 一致。

### 5.3 更新目标不可变

- 版本检查可以读取浮动频道，但实际安装必须锁定精确版本和精确 digest。
- 允许的镜像仓库只包括项目配置的官方 Docker Hub 和 GHCR 仓库。
- 拉取完成后必须校验 OCI 标签中的项目来源、版本和源码修订。
- 新容器必须使用 `image@sha256:...` 创建，不能在切换时再次解析 `rc` 或
  `latest`。
- 精确版本不能覆盖；相同版本但 digest 不同必须视为供应链异常并拒绝安装。
- 默认禁止降级。自动回滚只能回到本次升级前记录的旧镜像 ID。

### 5.4 更新期间不执行任务

升级不能与云盘转存并发发生。

管理员确认升级后：

```text
创建更新操作
    ↓
进入 UPDATE_DRAINING
    ↓
Scheduler 停止创建新任务
    ↓
Worker 停止领取新任务
    ↓
等待当前 RUNNING Task 结束
```

- 不得直接中断正在转存的文件；
- 有 RUNNING 或 CANCEL_REQUESTED Task 时必须等待；
- 等待超过配置上限后，本次升级失败，容器保持原状；
- 更新闸门必须持久化，API 重启后不得自动消失；
- 只有升级提交成功、回滚完成或管理员明确取消预检后才能解除闸门。

### 5.5 更新前数据快照

Updater 停止旧容器后，必须为下列内容创建同一时点的一致性快照：

- `/data/mediasync.db`；
- 同名 SQLite WAL/SHM 文件（如果存在）；
- `/data/config/runtime-secrets.json`；
- 当前 Alembic revision；
- 当前镜像 ID、digest、版本和容器配置摘要。

数据库和运行时密钥是不可分割的恢复单元。快照必须放在：

```text
/data/backups/updates/<operation_id>/
```

并使用同目录临时文件加原子重命名完成。快照目录权限不得放宽，不得把密钥值
写入操作日志或 API 响应。

### 5.6 临时 updater 助手

当前容器不能在停止自身后继续执行 Docker API。因此切换必须由临时 updater
助手完成：

```text
当前 MediaSync
    │
    ├── 拉取并校验目标镜像
    ├── 写入更新意图
    └── 创建 updater 助手容器
              ↓
         停止旧容器
              ↓
         创建一致性快照
              ↓
         以旧配置创建候选容器
              ↓
         验证或回滚
              ↓
         updater 自动删除
```

Updater 助手：

- 使用已验证的目标 MediaSync 镜像和专用 updater 命令；
- 只挂载 Docker Socket 和当前 `/data`；
- 不暴露端口；
- 不运行 API、Scheduler、Worker 或 Provider；
- 使用随机操作 ID 和不可预测容器名；
- 更新操作非终态期间使用 `unless-stopped`，不设置 `AutoRemove`；
- 持有 `/data/update/updater.lock` 的独占文件锁；
- 达到终态后先把自身 restart policy 改为 `no`，再退出并由 Appliance 清理；
- 不参与正常业务运行；
- 一次最多存在一个有效 updater；
- 退出前必须把执行状态写入 `/data/update/operations/<operation_id>.json`；只有完成
  数据库提交确认后才能写入 `SUCCESS` 终态。

Updater 保持 `NetworkMode=none`，不得为验证候选而临时加入业务网络、Host Network
或访问候选容器的公开端口。候选运行信息通过第 6.1 节定义的验证证据文件交付，
Docker Engine 中的容器状态和镜像 inspect 仍由 updater 直接读取。

详细的崩溃恢复、持久检查点、并发 helper fencing 和旧结果协议兼容见
[Updater 崩溃恢复协调器设计](updater-recovery-coordinator.md) 与 ADR-0008。

### 5.7 容器配置复制白名单

不得把 Docker inspect 的结果整体原样提交给 create API。新容器只能从白名单
复制：

- 原容器名称；
- `/data` 挂载；
- 用户显式配置的其他业务挂载；
- 端口绑定；
- 环境变量；
- 网络和网络别名；
- restart policy；
- DNS、时区、用户、用户组和只读文件系统设置；
- MediaSync 支持的设备映射；
- 与 MediaSync 有关且不由 Docker 生成的标签。

必须丢弃：

- 旧容器 ID、状态和进程信息；
- 旧镜像 ID；
- 临时 hostname、MAC、IP 和 sandbox ID；
- Docker 自动生成的挂载；
- updater 内部环境变量和凭证；
- Compose 管理字段；
- 任意新增的 privileged、capability 或宿主机挂载。

新容器的权限不得高于旧容器。升级流程不得顺带增加 `privileged`、Host Network
或其他宿主机目录。

## 6. 候选启动与提交

### 6.1 更新待提交标记

更新意图持久化在：

```text
/data/update/pending.json
```

候选容器发现该标记后进入更新验证模式：

- 正常执行配置读取、数据库迁移和旧任务对账；
- 启动 Nginx、API、Scheduler 和 Worker 进程；
- Scheduler 不创建任务；
- Worker 进程保持健康，但不领取任务；
- Provider 不执行网络副作用；
- API 只允许健康检查、登录和更新状态查询等安全操作。

这样 Docker 健康状态可以覆盖完整进程拓扑，但候选版本尚不能改变云盘远端状态。

### 6.1.1 候选验证证据

由于 updater 必须保持 `NetworkMode=none`，它不能依赖候选 HTTP API 作为验证通道。
候选容器在满足以下全部条件后，必须原子写入：

```text
/data/update/operations/<operation_id>.candidate.json
```

写入前提：

- `pending.json` schema、operation ID 和候选模式有效；
- `pending.json` 包含创建候选前生成的不可预测 candidate token；
- 候选实例 token 与 `pending.json` 中的 candidate token 一致；
- 数据库迁移已完成，能够读取当前 Alembic revision；
- Launcher、Nginx、API、Scheduler、Worker 五个组件全部健康；
- Scheduler 和 Worker 仍被候选验证闸门阻止产生远端副作用。

证据 schema v1 至少包含：

```json
{
  "schema_version": 1,
  "operation_id": "uuid",
  "candidate_token": "不可预测随机值",
  "mode": "candidate_validation",
  "version": "v0.3.0",
  "revision": "git commit sha",
  "digest": "sha256:...",
  "alembic_revision": "migration revision",
  "components": {
    "launcher": true,
    "nginx": true,
    "api": true,
    "scheduler": true,
    "worker": true
  },
  "observed_at": "UTC timestamp"
}
```

候选的版本、revision 和 digest 可以来自创建候选时注入的已验证元数据，但证据
文件不能成为这些字段的单独信任根。Updater 必须把它们再次与目标 handoff、候选
容器 inspect、不可变镜像引用和 OCI 标签交叉核对。

证据文件必须：

- 使用 0600 权限和同目录临时文件原子替换；
- 限制 schema、字段、文件大小和 operation ID；
- 不包含密码、环境变量、Cookie、Token 或 Docker inspect 原文；
- 通过 candidate token 防止同一 operation 的旧候选证据被复用；
- 在成功提交或回滚完成后删除。

不得为了候选验证新增无鉴权的版本接口，也不得把管理员 Session 或内部验证 token
暴露给浏览器。

### 6.2 成功条件

Updater 必须验证：

- 容器达到 Docker `healthy`；
- 五个 Appliance 组件全部健康；
- 候选证据中的 operation ID、candidate token、模式和五组件状态有效；
- 候选证据中的版本、源码修订和镜像 digest 与 handoff、容器 inspect 和 OCI
  标签一致；
- 数据库处于 Alembic head；
- 更新待提交模式仍然有效；
- 容器在稳定观察窗口内没有重启或子进程退出。

稳定观察窗口从 Docker 报告候选为 `healthy` 且有效候选证据写入后开始。观察期间
Updater 必须持续检查容器 ID、`RestartCount`、`StartedAt`、健康状态和候选证据；
任一项变化都应重新计时或判定失败，不能只读取一次健康结果后立即提交。

全部成功后：

1. Updater 原子写入非终态 `COMMIT_REQUESTED`；
2. 候选 Appliance 的更新对账器读取并严格验证提交请求；
3. 对账器在数据库事务中把活动更新操作推进到 `SUCCESS` 并释放活动槽；
4. 数据库提交成功后，对账器删除 `pending.json`、候选验证证据和已消费的
   handoff；
5. Updater 确认数据库状态为 `SUCCESS`、活动槽已经释放且三个运行标记都消失；
6. Updater 把结果文件从 `COMMIT_REQUESTED` 推进到 `SUCCESS`；
7. Updater 删除已停止的旧容器；
8. 保留更新快照；
9. Updater 自动退出并删除。

不能仅删除 `pending.json` 来表示提交成功。更新执行闸门还会根据数据库中的活动
更新操作阻止 Scheduler 和 Worker，必须先完成终态数据库事务。

`COMMIT_REQUESTED` 是不可回滚边界。进入该状态后，候选数据库可能随时完成提交，
Updater 不得再恢复旧数据库或启动旧容器。等待确认超时时必须保持候选运行、旧容器
停止，不得写入 `SUCCESS`，并保留可恢复现场。

### 6.2.1 终态对账握手

Updater 停止旧容器后不直接修改正在被候选使用的 SQLite 数据库。它通过：

```text
/data/update/operations/<operation_id>.json
```

发布 `COMMIT_REQUESTED` 提交请求，或 `ROLLED_BACK`、`ROLLBACK_FAILED` 回滚终态。
Appliance 在启动屏障后、允许 Scheduler 和 Worker 执行前运行更新对账器：

- 严格校验结果 schema、operation ID、合法状态转换和活动更新记录；
- 只有 `COMMIT_REQUESTED` 才能请求数据库进入 `SUCCESS`；
- 在单个数据库事务中写入对应终态并释放 `active_slot`；
- 数据库提交成功后再删除 `pending.json`、候选证据和已消费的 handoff；
- 删除标记失败时保持执行闸门关闭并在下次循环重试清理；
- 终态结果已对账时必须幂等返回，不能重复复用历史操作；
- `ROLLBACK_FAILED` 不自动解除闸门，必须保留人工恢复信息。

Updater 只有在观察到数据库终态和标记清理完成后，才把结果推进为 `SUCCESS`。
若等待超时，结果保持 `COMMIT_REQUESTED`，不能同时启动两个 MediaSync 容器，
也不能把尚未确认的状态报告为成功。Updater 或后续恢复协调器重启后必须先读取
数据库终态和运行标记：已提交则收敛到 `SUCCESS` 并清理旧容器，未确认则保持安全
停止并继续等待，禁止猜测性回滚。

### 6.2.2 重启策略 fencing

停止旧容器前，Updater 必须记录其原始 restart policy，并通过 Docker Engine 把旧
容器的 restart policy 临时改为 `no`。候选容器仍使用 handoff 中记录的原始策略。

这样即使 NAS 或 Docker daemon 在切换、验证或提交不确定阶段重启，也只能自动拉起
候选容器，不会同时拉起旧容器。只有在进入回滚路径、候选已经停止且旧数据快照恢复
完成后，Updater 才能恢复旧容器原始 restart policy 并启动旧容器。

若 restart policy fencing 未成功，Updater 不得停止旧容器。若 fencing 后 Updater
崩溃，恢复流程必须先根据结果状态和容器身份判断继续提交还是回滚，不能直接恢复旧
策略。

### 6.3 失败回滚

候选容器在超时、退出或健康失败，且结果尚未进入 `COMMIT_REQUESTED` 时：

1. 结果文件进入 `ROLLING_BACK`；
2. 停止并删除已创建的候选容器，但不删除卷；
3. 恢复数据库与运行时密钥快照；
4. 删除候选验证证据，但保留 `pending.json`、handoff、结果文件和快照；
5. 释放原容器名称，把旧容器恢复为原名称和原 restart policy；
6. 启动旧容器并验证身份、Docker 健康状态和五组件健康输出；
7. 写入 `ROLLED_BACK`，由旧版本 Appliance 对账并释放更新闸门；
8. 数据库终态提交后，由对账器清理 pending、候选证据和 handoff；
9. 旧版本也无法健康启动时写入 `ROLLBACK_FAILED`，保留人工恢复闸门；
10. 保留 updater 结果和快照供诊断。

恢复旧容器前必须恢复更新前数据库，不能让旧代码直接读取候选版本已经迁移的
数据库。

如果旧容器也无法恢复，Updater 不得继续重复重建。它必须保留快照、停止自动
操作，并输出可在 NAS 终端执行的恢复说明。

### 6.3.1 回滚终态对账

恢复快照后，数据库中的活动更新可能回到 `HANDOFF`、`SNAPSHOTTING`、
`SWITCHING`、`VERIFYING` 或 `ROLLING_BACK`。旧 Appliance 读取有效
`ROLLED_BACK` 结果后，必须在一个数据库事务内沿既有状态机补齐缺失状态并提交
`ROLLED_BACK`，同时释放 `active_slot`。不得从 `CHECKING`、`AVAILABLE`、
`PULLING` 或 `DRAINING` 接受该恢复终态。

`pending.json` 和 handoff 是结果文件与数据库操作之间的受信关联，在数据库终态
提交前不得删除。数据库提交成功后再执行文件清理；清理失败时执行闸门保持关闭，
后续对账必须能够幂等重试。

### 6.3.2 旧容器恢复证据

旧容器不能复用绑定目标镜像的候选证据。Updater 必须从 Docker inspect 交叉验证：

- 容器 ID 与 handoff 中记录的旧容器 ID 一致；
- 镜像 ID 与 handoff 中记录的源镜像 ID 一致；
- 容器处于运行状态且 Docker 健康状态为 `healthy`；
- 最新健康检查输出可以解析为对象；
- Launcher、Nginx、API、Scheduler、Worker 五个组件全部为 `true`；
- `RestartCount`、`StartedAt`、容器和镜像身份在稳定观察窗口内不变。

### 6.3.3 部分失败

- restart policy fencing 失败且旧容器仍运行时，不进入破坏性恢复步骤；
- 旧容器停止失败或结果不确定时，不得同时启动候选；
- 快照尚未完整创建时，不执行快照恢复；若旧数据未被候选打开，可恢复旧 restart
  policy 并重启旧容器；
- 候选尚未创建时跳过候选停止和删除；
- 旧容器尚未重命名时跳过名称恢复；
- 自动恢复最多尝试一次；任何无法确认的步骤进入 `ROLLBACK_FAILED`，不得循环重试。

进入 `COMMIT_REQUESTED` 后发生的确认超时、Updater 崩溃或网络错误不属于自动回滚
条件。此时恢复旧快照会与可能已经提交的候选数据库产生竞态，必须按第 6.2.1 节的
提交恢复规则收敛。

## 7. 更新状态机

数据库中的 `update_operations` 与 updater 结果文件承担不同职责，不能把两者合并成
一套状态后由两个进程共同写入。

数据库业务状态由 Appliance 独占写入：

```text
CHECKING
    ↓
AVAILABLE
    ↓
PULLING
    ↓
DRAINING
    ↓
HANDOFF
    ↓
SNAPSHOTTING
    ↓
SWITCHING
    ↓
VERIFYING
    ├──→ SUCCESS
    └──→ ROLLING_BACK
              ├──→ ROLLED_BACK
              └──→ ROLLBACK_FAILED
```

Updater 结果文件的成功路径为：

```text
SNAPSHOTTING -> SWITCHING -> VERIFYING -> COMMIT_REQUESTED -> SUCCESS
```

其中 `COMMIT_REQUESTED` 只属于 updater 结果协议，不写入
`update_operations.status`。Appliance 看到有效请求后，以单个数据库事务执行
`VERIFYING -> SUCCESS` 并释放 `active_slot`；Updater 观察到该事务和标记清理完成
后，再推进结果文件。

终态为：

- `SUCCESS`
- `FAILED`
- `ROLLED_BACK`
- `ROLLBACK_FAILED`
- `CANCELLED`

一次只能有一个非终态更新操作。终态操作记录不得被复用，敏感 Docker inspect
内容不得写入记录。

在 updater 结果协议中，`COMMIT_REQUESTED` 是非终态但不可回滚的提交不确定状态。
允许的转换只有：

- `VERIFYING -> COMMIT_REQUESTED`；
- `COMMIT_REQUESTED -> SUCCESS`。

不得定义 `COMMIT_REQUESTED -> ROLLING_BACK`。

## 8. API 契约

### 8.1 查询更新能力

```http
GET /api/v1/system/update
```

响应示例：

```json
{
  "current_version": "0.2.0-rc.9",
  "channel": "rc",
  "check_supported": true,
  "install_supported": true,
  "install_unavailable_reason": null,
  "docker_socket_enabled": true,
  "latest_release": {
    "version": "0.3.0",
    "digest": "sha256:...",
    "published_at": "2026-09-01T00:00:00Z",
    "requires_manual_upgrade": false
  },
  "operation": null
}
```

无 Socket 时 `check_supported` 仍为 `true`，`install_supported` 为 `false`，
并返回适合当前部署方式的人工升级说明。

### 8.2 检查新版本

```http
POST /api/v1/system/update/check
```

只有管理员可以主动刷新。后端必须缓存结果并限制频率，不能让前端刷新导致大量
GitHub 或 Registry 请求。

### 8.3 安装更新

```http
POST /api/v1/system/update/install
Content-Type: application/json
```

```json
{
  "version": "0.3.0",
  "digest": "sha256:...",
  "current_password": "管理员当前密码",
  "confirmation": "UPDATE"
}
```

安装要求：

- 有效管理员会话；
- 重新验证当前密码；
- 版本与最近一次可信检查结果一致；
- confirmation 精确匹配；
- 没有其他活动更新；
- 当前部署通过全部能力探测。

API 返回 `202 Accepted` 和操作 ID。容器切换期间 Web 连接中断属于预期行为，
前端应轮询健康入口并在新容器启动后恢复操作状态。

## 9. 前端交互

系统设置增加“版本与更新”区域：

- 当前版本、频道、最新版本和发布时间；
- 更新说明与重大变更警告；
- “检查更新”按钮；
- 无 Socket 时显示群晖、飞牛和 Docker 命令升级方法；
- 有 Socket 时显示“一键更新”按钮；
- 首次启用时展示 Docker 管理员权限风险；
- 安装前要求当前密码和文字确认；
- 显示下载、排空任务、切换、验证、回滚等阶段；
- 连接中断后自动等待新容器，不把中断直接显示为更新失败；
- 回滚成功后明确显示仍在旧版本；
- `ROLLBACK_FAILED` 时停止自动尝试并展示备份位置和人工恢复说明。

不得把 Docker Socket 路径、容器 inspect、环境变量值或 Registry 凭据显示在
浏览器中。

## 10. 日志与审计

结构化日志至少记录：

- 操作 ID；
- 当前版本和目标版本；
- 旧镜像 ID 和目标 digest；
- 状态转换；
- 每阶段耗时；
- 健康验证结果；
- 成功、回滚或人工介入结论。

不得记录：

- 管理员密码；
- Session Cookie；
- Docker Registry 凭据；
- 环境变量完整列表；
- `runtime-secrets.json` 内容；
- Provider Token；
- Docker inspect 原始响应。

## 11. 部署方式

默认安装保持不变：

```bash
docker run -d \
  --name mediasync \
  --restart unless-stopped \
  -p 9090:9090 \
  -v /你的路径/mediasync:/data \
  josephyjq/mediasync:latest
```

显式启用一键升级时增加：

```bash
-v /var/run/docker.sock:/var/run/docker.sock
```

Docker Hub、群晖和飞牛文档必须把这项挂载标记为“可选、高权限”。镜像元数据
不得把 Socket 声明为默认数据卷，NAS 图形安装表单不得自动添加这项挂载。

## 12. 实现拆分

设计合并后按以下边界创建独立 Issue：

1. 更新版本检查与只读 UI，不依赖 Docker Socket；
2. Docker 能力探测和当前容器唯一识别；
3. 更新状态存储、任务排空闸门和候选验证模式；
4. 目标镜像拉取、digest 与 OCI 标签校验；
5. 临时 updater 助手和容器配置白名单复制；
6. 数据快照、候选切换、健康提交和自动回滚；
7. Web 一键更新交互与断线恢复；
8. 群晖、飞牛、Docker 运维手册和故障演练。

每个 Issue 必须保持项目可构建、可测试、可发布。不得在第一个运行时 PR 中一次
实现完整升级链路。

## 13. 验收要求

实现完成前至少验证：

- 未挂载 Socket 时现有功能和安全边界不变；
- Socket 不可用、权限不足和当前容器识别失败时安全降级；
- 两个并发安装请求最多一个成功；
- 有运行中转存任务时不会停止旧容器；
- 拉取失败不会改变当前容器；
- digest 或 OCI 标签不匹配时拒绝安装；
- 新版本迁移失败后恢复旧数据库和旧容器；
- 新版本健康超时后自动回滚；
- Updater 崩溃后可以根据持久化状态恢复或安全停止；
- `COMMIT_REQUESTED` 等待超时时不会启动旧容器或伪造 `SUCCESS`；
- 数据库已提交但 Updater 未确认时，重启后可以收敛到 `SUCCESS`；
- 切换期间 NAS 或 Docker daemon 重启不会同时自动拉起新旧两个容器；
- 回滚过程中 NAS 重启后仍能识别未完成操作；
- 成功升级后端口、挂载、网络、环境变量和重启策略不变；
- updater 助手最终不存在；
- 群晖和飞牛的 amd64/arm64 镜像都通过真实升级演练；
- 精确镜像标签和数据快照可以完成一次人工恢复。

## 14. 风险与后续复审

Docker Socket 使 MediaSync 的漏洞影响范围扩大到宿主机 Docker。即使功能实现
正确，这个风险也不会消失。

出现以下情况时必须复审或禁用一键升级：

- 无法稳定唯一识别当前容器；
- NAS 平台对 Docker API 或容器重建行为不兼容；
- 升级回滚曾造成数据损坏；
- 安全审计发现 Socket 权限不可接受；
- Docker Engine 提供更小权限的原生更新接口；
- 群晖、飞牛普遍提供可被应用安全调用的官方更新 API。
