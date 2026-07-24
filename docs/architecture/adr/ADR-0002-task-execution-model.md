# ADR-0002：任务执行模型

- 状态：已接受
- 日期：2026-07-23
- 决策者：MediaSync 维护者
- 取代：无
- 被取代：无
- 相关内容：[Worker 与 Task Engine v2 架构](../worker-task-engine-v2.md)

## 背景

MediaSync v0.1 把 HTTP 请求处理、调度、扫描执行和转存执行都放在 FastAPI
进程中。该拓扑完成了 MVP 验证，但它把持久化同步任务绑定到了 API 生命周期，
同时让一条任务记录承担了队列状态、执行历史和日志等过多职责。

云盘操作还会跨越无法纳入同一事务的边界：远端操作可能已经成功，而 MediaSync
尚未提交本地结果。进程重启、网络异常、凭证过期和过期 Worker 都是正常运行
条件，不是罕见的边缘情况。

## 决策

MediaSync 使用持久化 Task Engine，并区分两个相关概念：

- **Task** 表示一个持久化业务意图，保存队列状态、重试策略、Payload 版本、
  幂等性、租约和当前所有权。
- **Task Run** 表示一次执行尝试，保存 Worker、fencing token、时间、结果、
  错误和指标。

执行边界如下：

```text
API       → 创建和查询任务
Scheduler → 创建到期的扫描任务
Worker    → 领取、执行、心跳、对账和结束任务
```

只有 Worker 可以执行后台同步。Scheduler 不得扫描或转存文件。

每次领取都会生成新的 `lock_token` 和 Task Run。活动所有者提交状态变更时，必须
匹配当前 token。租约允许系统在 Worker 消失后恢复任务；fencing 则阻止旧 Worker
在所有权变化后写入终态结果。

Provider 侧成功与 SQLite 提交不属于同一事务。因此，转存执行使用持久化幂等键
和远端对账，而不宣称提供严格的 exactly-once 交付。

## 不变量

### 数据不变量

- 每个 Task Payload 都有明确的 `payload_version`。
- Task 幂等键在对应业务操作范围内唯一。
- `(task_id, run_number)` 唯一，运行序号永不复用。
- 正常运行过程中不得删除 Task Run。
- 终态 Task Run 的核心执行结果不可变。

### 引擎行为不变量

- 一个 Task 同时最多只有一个活动租约所有者。
- 每次领取都生成新的、不可预测的 `lock_token`。
- 心跳和结束操作必须同时匹配 Task ID、所有者、状态和 `lock_token`。
- 旧 Worker 失去所有权后不得结束 Task。
- 重试必须创建新的 Task Run，并保留之前的尝试。
- 运行中取消先进入 `CANCEL_REQUESTED`；所有者完成远端对账后，再决定
  `SUCCESS` 或 `CANCELLED`。
- 无法自动刷新的凭证应把任务转为 `WAITING_CREDENTIAL`，而不是 `FAILED`，
  且不得消耗重试预算。

### Task Run 生命周期

活动 Task Run 可以在从 `RUNNING` 推进到 `SUCCESS`、`FAILED`、`CANCELLED`、
`BLOCKED` 或 `LOST` 等终态的过程中更新运行字段。

进入终态后：

- 运行序号永不复用；
- 正常处理过程中不得删除该记录；
- 核心结果、时间戳、执行结果和标准化错误不得修改；
- 后续尝试必须追加新的 Task Run。

### 运行时不变量

- 重启恢复必须保留被中断的 Run，并创建新的尝试。
- 重试必须使用有界退避，不得变成执行器进程内的 sleep 循环。
- 远端成功而本地失败时，再次复制前必须先对账。
- 凭证和原始 Provider 响应不得写入任务历史或日志。

## 后果

优点：

- API 或前端重启不会中断持久化执行；
- 每次尝试都可追踪，且不会让 Task 行承担全部历史；
- 所有权和旧写入都有明确的数据库条件；
- 重试、取消、凭证阻塞和恢复都有持久化状态；
- 未来 Provider 共享同一套可靠性契约。

成本：

- Task 和 Task Run 结构比 v0.1 更明确，也更复杂；
- Handler 必须返回标准化结果，不能直接赋值状态；
- 对账需要 Provider 提供相应能力；
- 迁移和状态机测试成为发布关键项。

## 已考虑的备选方案

### 继续在 FastAPI 中执行后台任务

拒绝。API 生命周期和部署变更仍会中断持久化任务。

### 在一条 Task 记录中同时保存队列与完整历史

拒绝。重试会覆盖证据，并把业务意图与单次执行尝试混在一起。

### 只依赖 `locked_by` 和租约过期

拒绝。暂停的 Worker 可能在租约过期后恢复，并覆盖新所有者的结果。因此每次
领取都必须有独立的 fencing token。

### 宣称转存具备 exactly-once 语义

拒绝。SQLite 和云盘 Provider 无法共享一个原子事务。MediaSync 提供
at-least-once 执行，并通过幂等和对账使远端副作用达到 effectively-once。

## 未来复审

只有新的设计 PR 用可测试的等价方案保留或替换现有可靠性保证时，才能取代本
决策。更换队列、数据库、工作流引擎或多 Worker 方案，本身并不能成为削弱
Task/Run 历史、租约所有权、fencing 或对账机制的理由。
