# ADR-0007：Updater 回滚由旧 Appliance 提交终态

- 状态：提议中
- 日期：2026-08-03
- 决策者：MediaSync 维护者
- 取代：无
- 被取代：无
- 相关内容：[Docker Socket 一键镜像升级设计](../docker-socket-one-click-update.md)、ADR-0005、ADR-0006、Issue #111

## 背景

Updater 失败回滚会恢复升级前的数据快照。快照中的 `update_operations` 必然仍是
提交前状态，通常为 `HANDOFF` 或 `SNAPSHOTTING`，不可能已经持久化
`ROLLING_BACK`。Updater 又不能直接写 SQLite，因此旧 Appliance 重启后必须负责把
恢复后的业务状态与 updater 的 `ROLLED_BACK` 结果收敛。

原设计还要求在启动旧容器前清理 `pending.json`。这样会同时移除旧 Appliance 定位
本次更新操作的受信关联，导致终态无法安全对账，活动槽也可能永久占用。

候选容器使用独立候选证据文件验证目标镜像身份。回滚验证面对的是已记录身份的旧
容器，不能复用包含目标版本和 candidate token 的候选证据。

## 决策

Updater 负责容器和快照恢复，旧 Appliance 独占数据库回滚终态提交。

自动回滚按以下顺序执行：

1. 结果文件进入 `ROLLING_BACK`；
2. 停止并删除已创建的候选容器，不删除卷；
3. 恢复已经校验的数据库与运行时密钥快照；
4. 删除候选证据，但保留 `pending.json`、handoff、结果文件和快照；
5. 释放原容器名称，恢复旧容器原名称和原 restart policy；
6. 启动旧容器并验证其身份、Docker 健康状态和五组件健康输出；
7. 结果文件进入 `ROLLED_BACK`；
8. 旧 Appliance 依据 pending、handoff 和结果文件完成数据库对账；
9. 对账器在一个数据库事务内把恢复出的提交前状态推进至
   `ROLLING_BACK -> ROLLED_BACK`，释放 `active_slot`；
10. 数据库提交成功后，对账器删除 pending、候选证据和 handoff，保留结果文件与
    快照。

数据库恢复状态允许为 `HANDOFF`、`SNAPSHOTTING`、`SWITCHING`、`VERIFYING` 或
`ROLLING_BACK`。对账器只能沿既有状态机向前补齐缺失的中间状态，并在同一事务中
提交 `ROLLED_BACK`；不得从更早的检查、拉取或排空状态接受 updater 回滚终态。

旧容器恢复验证使用 Docker inspect 的受限字段：容器 ID、源镜像 ID、运行状态、
`RestartCount`、`StartedAt`、健康状态和最新健康检查输出。健康检查输出必须能解析
为对象，且 Launcher、Nginx、API、Scheduler、Worker 五个组件均为 `true`。身份与
健康指纹必须在稳定观察窗口内保持不变。

## 不变量

- Updater 不直接写 SQLite，`active_slot` 只能由 Appliance 释放。
- `pending.json` 和 handoff 在数据库 `ROLLED_BACK` 提交前不得删除。
- 候选容器停止后才能恢复快照。
- 候选容器停止后才能恢复旧容器 restart policy 并启动旧容器。
- 旧容器必须匹配 handoff 中记录的容器 ID 与源镜像 ID。
- Docker `healthy` 不足以单独证明恢复成功，必须同时验证五组件健康输出。
- 自动回滚只执行一次；任一步骤无法确认完成时进入 `ROLLBACK_FAILED`。
- `ROLLBACK_FAILED` 保留 pending、handoff、结果文件和快照，不释放执行闸门。
- `COMMIT_REQUESTED` 之后永不进入本回滚流程。

## 后果

收益：

- 快照恢复后的数据库状态可以确定地释放活动槽；
- 旧 Appliance 的正常数据库所有权边界保持不变；
- 标记清理不会早于数据库终态提交；
- 回滚健康验证覆盖完整 Appliance 进程拓扑；
- 人工恢复现场不会被自动清理破坏。

成本：

- 终态对账器需要支持从多个合法提交前状态进行事务内收敛；
- Updater 需要解析 Docker 健康检查输出并维持稳定观察窗口；
- 部分失败必须记录已完成步骤，不能用一个无状态异常处理块实现；
- 旧镜像必须包含约定的五组件健康检查输出。

## 已考虑的备选方案

### Updater 直接把数据库写成 ROLLED_BACK

实现路径更短，但让 updater 获得 SQLite 业务状态写权限，与 ADR-0006 的所有权边界
冲突，因此拒绝。

### 启动旧容器前删除 pending

旧容器可以直接进入普通模式，但会丢失 operation 的受信关联，并可能在活动槽释放前
恢复任务副作用，因此拒绝。

### 只检查 Docker healthy

无法证明 Scheduler 或 Worker 等全部子进程健康，也不能排除错误容器复用了原名称，
因此拒绝。

### 复用候选证据文件

候选证据绑定目标版本、目标 digest 和 candidate token，与源镜像身份不匹配，因此
拒绝。

## 未来复审

出现以下情况时应复审本决策：

- 更新状态迁移改为独立事务日志而不再随业务数据库快照恢复；
- Docker 健康检查不再输出五组件状态；
- Updater 与 Appliance 之间引入新的受认证恢复通道；
- 真实 NAS 故障演练表明旧容器无法可靠提供当前健康证据。
