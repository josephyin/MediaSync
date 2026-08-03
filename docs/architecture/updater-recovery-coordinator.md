# Updater 崩溃恢复协调器设计

## 1. 目标与边界

本设计定义 updater 在进程、Docker daemon 或 NAS 重启后的恢复契约。目标是：

> 任意时刻中断后，只依据持久状态和 Docker 实际状态得到唯一恢复方向。

本阶段不实现安装 API、Web 按钮、真实 NAS 演练或多主机协调。默认部署仍是单 NAS、
单 Docker Engine、单 `/data`。

## 2. 进程所有权

### 2.1 持久 updater 容器

初始 Appliance 在停止自身前创建 updater 协调器：

- 固定命令：`python -m app.updater`；
- 只挂载 `/data` 与 Docker Socket；
- `NetworkMode=none`；
- `ReadonlyRootfs=true`；
- 不使用 `AutoRemove`；
- restart policy 为 `unless-stopped`；
- 标签必须包含 updater 角色和精确 operation ID。

协调器达到终态后必须：

1. 原子写入终态结果；
2. 把自身 restart policy 更新为 `no`；
3. 退出；
4. 由健康的 Appliance 删除这个已退出且标签完全匹配的协调器。

若第 2 步失败，协调器不得退出并制造重启循环；它进入有限间隔的终态清理等待并持续
暴露固定错误，等待 Appliance 或管理员清理。

每个新进程接管现有非终态时递增 `recovery_generation`。最大恢复代次固定为 3；超过
上限后，提交前或回滚中的操作进入 `ROLLBACK_FAILED`，`COMMIT_REQUESTED` 保持原
状态并转人工确认。协调器随后解除自身 restart policy，防止 Docker 无限重启。

### 2.2 独占锁

协调器启动后必须先打开：

```text
/data/update/updater.lock
```

要求：

- 路径不是符号链接；
- 文件权限为 `0600`，父目录为 `0700`；
- 使用非阻塞独占 `flock`；
- 文件描述符保持到进程退出；
- 锁内容只写 operation ID、协调器 ID 和取得时间，不作为所有权依据。

未取得锁的 helper 保持无副作用等待并定期重试，不得调用 Docker 写接口。当前所有者
退出并释放锁后，等待者必须先重新读取全部持久状态，不能沿用等待前的内存判断。
`flock` 不可用或 `/data` 无法证明为本地文件系统时，一键更新能力探测返回不可用。

协调器通过容器内 hostname 取得自身短 ID。短 ID 必须是 12 至 64 位小写十六进制，
并且只能前缀匹配一个带当前 operation 标签、专用命令和受限挂载的 updater 容器。
无法唯一确认自身容器时，不得修改 restart policy 或退出。

## 3. 持久化文件

每次操作使用：

```text
/data/update/pending.json
/data/update/updater.lock
/data/update/operations/<operation_id>.handoff.json
/data/update/operations/<operation_id>.candidate.json
/data/update/operations/<operation_id>.json
/data/backups/updates/<operation_id>/manifest.json
```

所有文件继续使用同目录临时文件、`fsync` 和原子替换。恢复协调器不得扫描并执行任意
文件名；operation ID 必须来自受限环境变量，并与 handoff、pending、结果文件和 Docker
标签交叉验证。

## 4. Updater 结果协议 v2

schema v2 记录最新可恢复状态：

```json
{
  "schema_version": 2,
  "operation_id": "uuid",
  "sequence": 9,
  "status": "switching",
  "checkpoint": "candidate_started",
  "recovery_generation": 1,
  "source_container_id": "64-char-id",
  "source_image_id": "sha256:...",
  "source_container_name": "MediaSync",
  "target_image": "repository@sha256:...",
  "target_revision": "git-sha",
  "candidate_token_hash": "sha256:...",
  "candidate_container_id": "64-char-id",
  "rollback_started": false,
  "updated_at": "UTC timestamp",
  "error_code": null,
  "public_error_message": null
}
```

`recovery_generation` 在新进程取得独占锁并接管非终态操作时递增，只用于审计和拒绝旧
内存对象；真正的进程互斥由 `flock` 保证。

源容器 ID、源镜像 ID、原始名称、目标不可变镜像和目标 revision 在 schema v2 创建后
不可修改。candidate token 只在 pending 和候选环境中保存原值，结果文件保存其
SHA-256，用于 pending 删除后的身份核对。

每次接管、检查点推进或状态转换都必须使 `sequence` 精确加一。接管可以保持原 status
和 checkpoint，但只能增加 `recovery_generation`。终态结果继续不可修改；终态后的
容器清理通过实际状态幂等完成，不回写伪造的新检查点。

### 4.1 正常路径检查点

检查点按以下顺序单调推进：

```text
initialized
old_restart_fenced
old_stopped
pending_ready
snapshot_verified
old_renamed
candidate_created
candidate_started
candidate_verified
commit_requested
```

从 `candidate_created` 开始必须记录完整 `candidate_container_id`。状态与检查点必须匹配：

- `SNAPSHOTTING`：`initialized` 至 `snapshot_verified`；
- `SWITCHING`：`old_renamed` 至 `candidate_started`；
- `VERIFYING`：`candidate_started` 或 `candidate_verified`；
- `COMMIT_REQUESTED`：只允许 `commit_requested`。

schema v2 必须在任何 Docker 写调用前，以 `initialized` 创建，并从 handoff 固化源容器
和目标镜像身份。写入 pending 后记录 candidate token 的哈希；原 token 不复制到结果
文件或日志。

### 4.2 回滚检查点

`rollback_started=true` 后只允许以下单调顺序：

```text
rollback_started
candidate_stopped
candidate_removed
snapshot_restored
candidate_evidence_removed
old_name_restored
old_policy_restored
old_started
old_verified
rollback_published
```

不存在的候选容器、尚未完成的快照或从未发生的重命名可以被验证为“不需要”，但也要
原子记录相应检查点，避免新进程再次猜测。

## 5. 身份发现

### 5.1 旧容器

旧容器只能通过 handoff 中的完整容器 ID 识别，并交叉验证源镜像 ID、数据挂载和原始
名称。名称本身不是身份。

### 5.2 候选容器

候选 create config 新增：

```text
io.mediasync.update.role=candidate
io.mediasync.update.operation=<operation_id>
```

恢复时必须同时匹配：

- v2 结果中的 candidate container ID，或在 ID 尚未落盘时得到唯一候选；
- operation 与 role 标签；
- 原始业务容器名称；
- `image@digest`；
- `/data` 挂载；
- candidate token 环境变量。

零个候选按检查点决定尚未创建或已删除；多个候选、身份冲突或 ID 不一致直接进入人工
恢复。

### 5.3 updater 协调器

协调器必须同时匹配 updater 角色、operation 标签、专用命令、目标镜像和受限挂载。
同一 operation 可以暂时存在多个 helper 容器，但只有持有 `flock` 的进程拥有执行权。

## 6. 恢复决策矩阵

| 持久状态 | 恢复方向 | 禁止行为 |
|---|---|---|
| 无结果文件 | 仅在旧容器身份和运行状态完整时开始新执行 | 不依据容器名称猜测 |
| `SNAPSHOTTING` 且旧容器未停 | 核对 fencing 后继续停止；无法确认则人工恢复 | 不创建候选 |
| `SNAPSHOTTING` 且旧容器已停 | 补齐恢复关联后开始同一次自动回滚 | 不继续向目标版本提交 |
| `SWITCHING` / `VERIFYING` | 开始同一次自动回滚 | 不复用旧健康观察直接提交 |
| `ROLLING_BACK` | 按检查点和实际状态续跑原回滚 | 不增加第二次回滚尝试 |
| `COMMIT_REQUESTED` | 只启动或保持候选并等待数据库成功收敛 | 不恢复快照、不启动旧容器 |
| `SUCCESS` | 幂等删除严格匹配且已停止的旧容器与 helper | 不修改候选和数据库 |
| `ROLLED_BACK` | 等待旧 Appliance 数据库对账并清理 helper | 不重新执行快照恢复 |
| `FAILED` / `ROLLBACK_FAILED` | 只读诊断和人工恢复 | 不自动改变容器或文件 |

### 6.1 COMMIT_REQUESTED

恢复协调器依次判断：

1. 数据库已是 `SUCCESS`、`active_slot` 已释放且运行标记已清理：把 v2 结果推进为
   `SUCCESS`，删除严格匹配的旧容器；
2. 数据库仍为 `VERIFYING` 且 pending、handoff、候选证据有效：确保候选是唯一活动
   Appliance，继续等待对账；
3. 候选已停止但身份完全匹配：只允许启动候选，不允许启动旧容器；
4. 数据库、标记或容器身份冲突：保持现场并进入人工恢复，不猜测提交结果。

### 6.2 提交前恢复关联

初始 Appliance 只有在数据库操作至少进入 `HANDOFF` 且目标版本、digest 与 handoff
一致后，才允许创建 updater。旧容器已经停止但 pending 尚未写入时，schema v2 恢复
协调器可以在严格验证数据库活动操作、handoff 和结果身份完全一致后生成新的 candidate
token、写入 pending，并立即进入回滚。这个 pending 只用于让旧 Appliance 安全提交
`ROLLED_BACK`，不得再创建候选。

### 6.3 ROLLING_BACK

已进入回滚的操作只续跑原尝试。每一步先观察后执行：

- 候选已经停止或删除时直接确认检查点；
- 快照恢复可以对同一不可变 manifest 幂等重放；
- 名称、restart policy 和启动状态必须从 Docker inspect 确认；
- 旧容器验证仍使用 ADR-0007 的身份、稳定窗口和五组件健康输出；
- 任一步骤出现多解、身份冲突或不支持的错误，写入 `ROLLBACK_FAILED` 并停止。

## 7. Schema v1 兼容

schema v1 不重写为伪造的 v2 历史：

- `SUCCESS`、`ROLLED_BACK`：只有仍存在有效 handoff 并能得到唯一容器身份时才执行
  幂等清理，否则保留可能的孤儿容器并提示人工检查；
- `COMMIT_REQUESTED`：允许依据数据库、运行标记和唯一候选继续成功收敛；
- `FAILED`、`ROLLBACK_FAILED`：保持人工恢复；
- `SNAPSHOTTING`、`SWITCHING`、`VERIFYING`、`ROLLING_BACK`：由于缺少检查点，停止
  自动副作用并报告旧协议人工恢复。

项目在生产入口开放前不会产生真实 schema v1 更新操作，因此该策略优先失败关闭，
不为未发布行为增加猜测性兼容。

## 8. 故障注入验收

实现 PR 必须在每个副作用“调用前、调用成功后但检查点写入前、检查点写入后”注入
中断，并重新创建协调器验证收敛。至少覆盖：

- restart-policy fencing；
- 旧容器停止和重命名；
- pending 与快照；
- 候选创建、启动和验证；
- `COMMIT_REQUESTED` 前后；
- 候选停止和删除；
- 快照恢复；
- 旧容器名称、策略、启动和健康验证；
- 终态后 helper 自身 restart policy 解除；
- 两个 helper 并发启动；
- Docker daemon 与 NAS 重启后的容器集合。

## 9. 实现拆分

设计合并后按小 PR 实现：

1. updater 结果 schema v2、检查点验证和 v1 只读兼容；
2. updater 独占文件锁和持久 helper 容器契约；
3. Docker 身份发现与恢复决策器；
4. 正常路径改为检查点驱动；
5. 回滚路径改为检查点驱动并支持续跑；
6. `app.updater` 可执行入口与终态 helper 清理；
7. Docker / NAS 故障演练后，才开放安装 API。
