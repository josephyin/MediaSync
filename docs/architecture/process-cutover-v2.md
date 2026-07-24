# 进程拓扑切换 v2

- 状态：提议中
- 目标里程碑：v0.2 Foundation
- 范围：兼容模式、旧任务对账、进程启用、Docker Compose 启动顺序、升级与回滚
- 扩展：[Worker 与 Task Engine v2 架构](worker-task-engine-v2.md)
- 依据：[ADR-0001](adr/ADR-0001-single-worker-on-sqlite.md) 和
  [ADR-0002](adr/ADR-0002-task-execution-model.md)
- 最后更新：2026-07-24

## 决策摘要

MediaSync 在修改官方 Compose 拓扑前，先采用分阶段兼容模式。运行时代码首先
同时具备互斥的 `legacy` 和 `process` 行为，而应用默认值仍为 `legacy`。后续
Compose PR 再显式选择 `process`，并在一个 API、一个 Scheduler 和一个 Worker
启动前，依次运行一次性迁移与对账 barrier。

旧执行器与 v2 执行器绝不能并发消费同一个 SQLite 队列。v2 Worker 启动前必须
对持久化的旧执行状态进行对账。v2 执行过任务后，只能回滚到具备兼容能力的版本，
或恢复切换前数据库备份。

本文进一步明确父级架构中的实现顺序：必须先完成进程切换及可靠性验证，再开始
Provider SDK v2。本文不修改长期的 Provider 拆分决策。

## 1. 背景

MediaSync 已分别实现并测试了 Task Engine v2 数据模型、Repository、租约恢复、
Worker 运行时、扫描与转存 Handler、定时扫描入队操作和 Scheduler 运行时。

默认部署仍使用 v0.1 执行拓扑：

```text
FastAPI
├── API
├── APScheduler 订阅任务
├── FastAPI BackgroundTasks 扫描
└── APScheduler 转存轮询
```

新命令已经存在，但 Docker Compose 尚未启动它们：

```text
python -m app.scheduler
python -m app.worker
```

下一步不只是修改 Compose。直接切换可能导致旧执行器与 Task Engine v2 Worker
同时运行，也可能让没有租约的旧 `RUNNING` 任务永久搁置、重复推进订阅计划，
或让 API 手动创建的任务在没有消费者时一直排队。

本文在修改运行时行为之前定义切换契约。文中的 **必须**、**不得**、**应该**
和 **可以** 均为规范性要求。

## 2. 目标与非目标

### 2.1 目标

切换必须：

- 把后台执行移出 API 进程；
- SQLite 方案只启动一个 Scheduler 和一个 Worker；
- 保留排队任务和执行历史；
- 防止旧执行器与 v2 执行器重叠运行；
- Worker 启动前对持久化旧执行状态进行对账；
- 保证任务创建和计划推进持久化；
- 提供确定性的升级与回滚顺序；
- 保持每个实现 PR 可构建、可测试、可发布。

### 2.2 非目标

本次切换不包含：

- 新增 Provider；
- 引入 Redis、消息代理或 Kubernetes；
- 在 SQLite 上支持多个 Worker 或 Scheduler；
- 实现 Provider SDK v2；
- 实现持久化进程心跳或最终版健康检查 API；
- 立即删除全部兼容代码；
- 重新设计 Web UI。

## 3. 运行时兼容模式

兼容版本必须增加一个共享配置：

```text
BACKGROUND_EXECUTION_MODE=legacy | process
```

兼容期内应用默认值保持 `legacy`，官方切换后的 Compose 明确设置为 `process`。

同一部署中的所有后端容器必须使用相同配置值。在同一个 SQLite 数据库上混用
不同模式不受支持，并且必须作为运维配置错误明确提示。

### 3.1 Legacy 模式

在 process 模式实现和验证期间，`legacy` 保留当前部署行为。

legacy 模式下：

- API 启动进程内 APScheduler；
- 订阅任务可以直接执行扫描；
- 手动扫描可以使用现有兼容执行路径；
- 旧转存轮询器继续运行；
- 独立 Scheduler 和 Worker 命令必须拒绝启动。

拒绝独立命令是一道安全围栏，防止运维人员误把 v2 Worker 与旧转存轮询器同时
启动。

### 3.2 Process 模式

process 模式下：

- API 不得启动 APScheduler；
- API 不得使用 FastAPI `BackgroundTasks` 执行同步；
- 订阅 CRUD 不得增删改 APScheduler 任务；
- 手动扫描和文件重试只能创建任务；
- Scheduler 只能创建到期的 Scan 任务；
- Worker 是唯一的扫描与转存执行器；
- 只有 Scheduler 可以推进周期性 `next_scan_at`；
- 只支持一个 Scheduler 和一个 Worker。

### 3.3 不重叠不变量

禁止出现以下拓扑：

```text
旧 APScheduler 转存轮询器
              +
Task Engine v2 Worker
              =
两个执行器消费同一个队列
```

官方部署必须使用一个共享模式配置和互斥的服务命令。升级说明必须要求先停止旧
服务，再启动任何 process 模式服务。

模式配置只是部署围栏，不是分布式选主。它无法保证分别配置的容器安全运行。
支持独立编排器需要基于数据库的运行代次或 Leader 租约，不属于 v0.2 范围。

## 4. Process 模式 API 契约

### 4.1 手动扫描

`POST /subscriptions/{id}/scan` 必须：

1. 校验订阅和冷却策略；
2. 查找该订阅已有的非终态 Scan 任务；
3. 如果存在，返回该任务；
4. 否则创建一个 `PENDING` Scan v1 任务，其 Payload 为：

```json
{
  "force_full": false
}
```

5. 根据请求设置 `force_full`；
6. 提交事务后再返回 HTTP `202`；
7. API 进程不得执行任何 Provider 或扫描操作。

活动集合包括 `RETRY`、`WAITING_CREDENTIAL` 和 `CANCEL_REQUESTED` 等所有非终态，
不能只检查 `PENDING` 和 `RUNNING`。

已完成的手动扫描不应阻止用户以后再次主动扫描。某次扫描仍处于活动状态时，
重复请求必须返回同一个任务。

### 4.2 文件重试

`POST /files/{id}/retry` 不得把终态任务重新改成 `PENDING`。

它必须：

1. 文件存在非终态 Transfer 任务时返回该任务；
2. 否则创建新的后继 Transfer v1 任务；
3. 保留终态前驱任务及其全部 Task Run；
4. 由文件和前驱任务推导后继幂等键，使重复 HTTP 请求返回或冲突到同一后继；
5. 后继任务持久化成功后，才清除文件的展示错误。

后继幂等键示例：

```text
transfer-retry:{file_id}:{predecessor_task_id}
```

如果后继任务也进入终态，下一次重试以该后继作为新的前驱，因此会获得新键。

### 4.3 任务变更

API Router 必须使用 Task Engine 入队服务或命令服务，不得直接给任务状态字符串
赋值。

取消和凭证唤醒仍是独立 Task Engine 命令，本次进程切换不会隐式增加它们。

## 5. 计划所有权

不同兼容模式下，计划所有权如下：

| 模式 | 周期性 `next_scan_at` 的所有者 |
|---|---|
| `legacy` | 兼容扫描路径和 APScheduler 任务管理 |
| `process` | Scheduler 入队事务 |

process 模式下，Scan 执行不得移动 `next_scan_at`。该要求同时适用于定时和手动扫描：

- 定时扫描在任务入队时已经完成计划记账；
- 手动扫描不得推迟下一次周期扫描；
- 扫描失败不得造成 Scheduler 快速循环。

Scheduler 按定时扫描入队契约，从当前调度时间计算下一次执行，以合并停机期间
错过的计划。

扫描领域代码中的兼容分支必须隔离并有测试覆盖，观察期结束后删除。

## 6. 旧任务对账

数据库可能保存由兼容运行时创建或启动的任务。对账在 Alembic 迁移后、API、
Scheduler 和 Worker 启动前运行。

对账命令必须：

- 具有事务性；
- 可幂等重跑；
- 中断后安全重试；
- 不调用 Provider；
- 明确处理每种持久化任务状态；
- 必要时用标准化错误码和 Task Run 历史记录结果。

### 6.1 状态处理

| 持久化状态 | 切换处理方式 |
|---|---|
| `PENDING` | 保留；Worker 可以领取。 |
| `RETRY` | 保留 `next_attempt_at`；到期后由 Worker 领取。 |
| `WAITING_CREDENTIAL` | 保留；不得轮询 Provider。 |
| 无所有权的 `CANCEL_REQUESTED` | 保留；由 Worker 领取并对账。 |
| 具备完整 v2 所有权和租约的 `CANCEL_REQUESTED` | 保留；重启后交给正常租约恢复。 |
| `SUCCESS` | 作为终态历史保留。 |
| `FAILED` | 作为终态历史保留。 |
| `CANCELLED` | 作为终态历史保留。 |
| 具备完整 v2 所有权和租约的 `RUNNING` | 保留；重启后交给正常租约恢复。 |
| 缺少完整 v2 所有权的 `RUNNING` | 按旧孤儿任务对账。 |

### 6.2 旧孤儿任务

如果 `RUNNING` 任务没有完整的 `locked_by`、`lock_token`、`locked_at` 和
`lease_until` 元组，则它并非由 Task Engine v2 领取。

对每个旧孤儿任务，对账必须：

1. 对齐 v2 重试计数与兼容尝试计数，且不得降低任何历史值；
2. 没有等价终态尝试记录时，追加或结束一条合成/丢失 Task Run；
3. 使用错误码 `LEGACY_CUTOVER_RECOVERY`；
4. 清理不完整的所有权字段；
5. 有剩余重试预算时，把任务移到 `RETRY`，并设置有界的近期
   `next_attempt_at`；
6. 否则转为终态 `FAILED`；
7. 保留订阅、文件、消息、时间戳和此前 Run。

对账不得声称远端操作已经失败。旧 Transfer 可能在进程停止前已在远端成功。

### 6.3 远端副作用边界

启用 process 切换前，Transfer v1 Handler 必须通过以下测试：

- 文件记录显示此前已保存；
- 上一次结果未知，但目标项目已经存在；
- 远端成功后本地提交失败；
- 重试不会创建第二份远端副本。

如果当前 Provider 契约无法可靠对账，必须暂停切换，先设计 Provider 对账能力，
不得默认启用 Worker。

Scan 重试可以从持久化文件指纹和目录检查点继续，但任务与 Run 历史仍须记录
丢失的旧尝试。

## 7. 迁移与服务 barrier

此前后端镜像在 API 命令中执行 `alembic upgrade head`，API lifespan 还会调用
`Base.metadata.create_all()`。三个进程独立启动时，这种方式并不安全。

process 模式 Compose 必须增加一次性 barrier：

```text
mediasync-migrate
    alembic upgrade head
            |
            v
mediasync-cutover
    对账旧任务
            |
            +------------------+------------------+
            |                  |                  |
            v                  v                  v
     mediasync-api      mediasync-scheduler  mediasync-worker
```

API、Scheduler 和 Worker 只能在迁移与对账成功后启动。任一一次性服务失败，都
必须阻止正常处理。

Scheduler 与 Worker 的相对启动顺序不是正确性要求，因为任务队列已经持久化。
可以先启动 Scheduler，但两者都必须等待相同的迁移 barrier。

process 模式下：

- API 命令只运行 Uvicorn；
- Scheduler 命令为 `python -m app.scheduler`；
- Worker 命令为 `python -m app.worker`；
- 所有服务挂载同一个 SQLite 数据卷；
- 除成功完成的一次性服务外，所有服务都使用 `restart: unless-stopped`；
- 必须从生产启动流程移除 `Base.metadata.create_all()`；
- Alembic 是唯一数据库结构迁移权威。

SQLite 数据库必须位于具备可靠文件锁的本地文件系统。Worker 副本数和并发数
都为一。

## 8. 升级顺序

受支持的 NAS 升级步骤：

1. 完全停止现有 MediaSync 服务。
2. 确认没有旧 API 或手动启动的 Worker/Scheduler 进程残留。
3. 创建带时间戳的 SQLite 备份，并记录应用版本和 Alembic 版本。
4. 拉取或构建具备兼容能力的镜像。
5. 运行 `mediasync-migrate`，要求退出码为 `0`。
6. 运行 `mediasync-cutover`，要求退出码为 `0`。
7. 使用 `BACKGROUND_EXECUTION_MODE=process` 启动 API、Scheduler 和唯一 Worker。
8. 检查 API/数据库健康状态和进程启动日志。
9. 手动触发一次扫描，确认由 Worker 执行。
10. 确认 Scheduler 为一个到期订阅创建任务。
11. 确认不存在旧 APScheduler 或转存轮询器的启动日志。
12. 在整个观察期内保留备份。

新旧拓扑之间不得滚动重启，必须安排短暂的全服务维护窗口。

## 9. 回滚契约

回滚存在两个不同边界。

### 9.1 Process 执行前

如果迁移或对账在 API、Scheduler 和 Worker 启动前失败：

- 停止新服务；
- 修正配置或恢复升级前备份；
- 以 `legacy` 模式回到最近的兼容版本。

此阶段尚未产生新的 Provider 副作用。

### 9.2 Process 执行后

v2 Worker 执行过任何任务后，不得直接回到无法理解 v2 状态、不可变 Payload、
Task Run 或后继重试语义的 v0.1 程序。

受支持的选择为：

1. 停止所有 process 模式服务，在明确受支持的数据库结构上，以 `legacy` 模式
   运行具备兼容能力的版本；或
2. 停止全部服务并恢复切换前 SQLite 备份，同时接受备份后 MediaSync 历史丢失。

恢复数据库不会撤销远端云盘操作。触发重试前，运维人员必须先对账备份后保存的
文件。

每个发布说明都必须注明与当前 Alembic 版本兼容的最早应用版本。不能仅因为
Alembic 存在 downgrade 函数，就假定数据库降级安全。

## 10. 切换期间的可观测性

兼容版本必须记录：

- 选中的后台执行模式；
- 是否启动或抑制进程内 APScheduler；
- Scheduler 和 Worker 的启动与停止；
- 按处理结果分类的旧任务对账数量；
- Scheduler 入队数量；
- Worker 任务 ID、Run ID 和所有权丢失；
- process 命令在 legacy 模式启动时的配置拒绝。

日志不得包含 refresh token、Cookie、解密凭证、分享密码或原始 Provider 响应。

持久化进程心跳和最终复合健康响应属于后续 v0.2 可观测性 Issue。即使暂未实现，
也不能允许进程静默启动失败；切换版本必须配置 Docker 重启策略并输出明确启动
日志。

## 11. 验证矩阵

自动化测试必须覆盖：

| 场景 | 要求结果 |
|---|---|
| Legacy 模式 API 启动 | APScheduler 启动；独立进程命令拒绝运行。 |
| Process 模式 API 启动 | 不启动 APScheduler 或转存轮询器。 |
| Process 模式手动扫描 | HTTP `202`；持久化 Scan v1 任务；无 BackgroundTask。 |
| 重复手动扫描 | 返回已有非终态任务。 |
| 重试终态转存 | 创建新后继任务；前驱及其 Run 不变。 |
| 旧孤儿 Scan | 记录丢失尝试；任务变为可重试或失败。 |
| 旧孤儿 Transfer | 记录丢失尝试；重试前必须对账。 |
| 迁移/对账失败 | API、Scheduler 和 Worker 保持停止。 |
| 转存期间 API 重启 | Worker 继续并完成任务。 |
| Scheduler 重启 | 到期扫描最终只入队一次。 |
| Worker 重启 | 租约恢复保留 fencing 和 Run 历史。 |
| 手动扫描与定时触发同时发生 | 最多一个活动 Scan 任务。 |
| 定时扫描完成 | `next_scan_at` 不会再次推进。 |
| NAS 完整重启 | 非终态任务恢复，且不重复转存。 |
| 错误配置额外 Worker | 部署验证拒绝该拓扑。 |

候选发布还必须使用数据库副本完成真实 Compose 冒烟测试：

```text
migrate -> reconcile -> api + scheduler + worker
```

冒烟测试不仅检查 HTTP 健康状态，还必须检查进程列表和日志。

## 12. 分阶段实现

实现遵循小而可审查的变更原则：

### PR A——兼容模式与安全围栏

- 增加并校验 `BACKGROUND_EXECUTION_MODE`；
- 记录所选模式；
- 让独立 Scheduler/Worker 在 legacy 模式拒绝启动；
- 默认值保持 `legacy`；
- 增加模式矩阵测试；
- 不修改官方 Compose 行为。

### PR B——Process 模式 API 与计划所有权

- process 模式下手动扫描改为入队；
- 创建后继 Transfer 重试；
- process 模式下停止修改订阅 APScheduler 任务；
- Scheduler 成为 `next_scan_at` 的唯一 process 模式所有者；
- 保留经过测试的 legacy 分支；
- 官方 Compose 仍保持 legacy 模式。

### PR C——旧任务对账与启动 barrier

- 增加幂等对账命令；
- 覆盖所有持久化任务状态；
- 从生产启动流程移除 `Base.metadata.create_all()`；
- 分离迁移、对账和常驻进程命令；
- 官方 Compose 仍保持 legacy 模式。

### PR D——官方 Compose 切换

- 增加一次性迁移和对账服务；
- 增加 API、Scheduler 和唯一 Worker 服务；
- 将共享模式设置为 `process`；
- 增加服务依赖和副本限制；
- 执行 Compose 和 NAS 重启验收测试。

### PR E——观察与移除兼容代码

- 至少观察一个发布周期，或完成明确记录的维护者 soak 周期；
- 修复可靠性问题，不新增 Provider；
- 只有回滚窗口关闭后，才移除旧 APScheduler、BackgroundTasks 和转存轮询代码。

只有进程切换验收矩阵通过后，才能开始 Provider SDK v2。

## 13. 完成条件

满足以下条件后，进程切换才算完成：

- 官方 Compose 运行一个 API、一个 Scheduler 和一个 Worker；
- API 中没有活动同步执行器；
- 受支持配置下，不可能同时启动旧执行器和 v2 执行器；
- 旧任务对账可重复执行且测试完整；
- 手动扫描和重试使用持久化 v2 任务；
- 定时扫描只有一个计划所有者；
- API 重启不会中断执行；
- NAS 重启可以恢复非终态工作；
- 结果未知的转存可以对账，且不会产生远端重复副本；
- 备份、升级和回滚说明已经在数据库副本上实际演练。
