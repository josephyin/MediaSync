# ADR-0008：Updater 使用持久协调器与可收敛检查点

- 状态：已接受
- 日期：2026-08-03
- 决策者：MediaSync 维护者
- 取代：无
- 被取代：无
- 相关内容：[Updater 崩溃恢复协调器设计](../updater-recovery-coordinator.md)、ADR-0005、ADR-0006、ADR-0007、Issue #114

## 背景

ADR-0005 把 updater 定义为启用 `AutoRemove` 的临时助手。旧容器完成 restart-policy
fencing 后，如果 NAS、Docker daemon 或 updater 进程在候选容器创建前中断，旧容器
不会自动启动，updater 也不会回来，系统中可能没有任何进程能够继续恢复。

现有 updater 结果 schema v1 只记录阶段状态。它没有候选容器 ID、最后完成的破坏性
步骤或回滚是否已经开始。新进程不能区分“步骤尚未执行”和“步骤已完成但结果文件
尚未更新”，直接重跑状态机可能重复恢复、同时启动两个 Appliance 或把提交不确定
误判为回滚条件。

## 决策

一次更新操作使用一个可随 Docker daemon 重启的持久 updater 协调器容器。操作期间
协调器不启用 `AutoRemove`，使用 `unless-stopped` restart policy。达到
`SUCCESS`、`ROLLED_BACK`、`FAILED` 或 `ROLLBACK_FAILED` 后，协调器先把自身
restart policy 改为 `no`，再退出；活动 Appliance 根据严格标签删除已退出的协调器。

协调器对 `/data/update/updater.lock` 持有进程生命周期级独占 `flock`。等待锁的重复
协调器不得读取后继续执行，更不得调用 Docker 写接口。锁文件必须是本地 POSIX 文件
系统上的普通私有文件；无法证明锁语义时禁用一键更新。

updater 结果协议升级为 schema v2，在原有业务状态之外持久化：

- 单调递增的 `sequence`；
- 当前 `checkpoint`；
- 恢复代次 `recovery_generation`；
- 当前 `coordinator_container_id`；
- 源容器、源镜像和目标镜像的不可变身份；
- 已创建的 `candidate_container_id`；
- 回滚是否已经开始；
- 固定错误代码和可公开错误信息。

每个 Docker 或文件系统副作用必须可通过严格观察确认，并在完成后原子推进检查点。
进程在副作用与检查点之间崩溃时，新协调器先核对 handoff、结果、快照 manifest、
pending 和 Docker 实际状态；只有结果唯一时才把该步骤收敛为已完成。

旧容器已经停止后、提交请求前发生进程中断，恢复协调器不继续向目标版本提交，而是
进入或续跑同一次自动回滚。进入 `COMMIT_REQUESTED` 后只允许向成功提交收敛，永不
恢复旧快照。

## 不变量

- 同一 `/data` 同时最多一个协调器持有 updater 独占锁。
- 未持有锁的进程不得执行任何更新副作用。
- updater 容器在非终态期间必须能够随 Docker daemon 重启。
- 每个副作用必须可观测、可验证且幂等，或在不确定时失败关闭。
- `COMMIT_REQUESTED` 之后不得恢复旧快照或启动旧容器。
- `ROLLING_BACK` 重启只能续跑原回滚，不得创建新的自动回滚尝试。
- 恢复代次有固定上限，不能依赖 Docker restart policy 无限重启。
- 没有持久化证据且无法从实际状态唯一推导时，必须进入人工恢复。
- updater 不直接写 SQLite，数据库终态仍由 Appliance 独占提交。
- 协调器终态后必须先解除自身自动重启，再允许清理容器。

## 后果

收益：

- NAS 和 Docker daemon 重启后仍有进程负责恢复；
- 崩溃恢复依据持久检查点与实际状态，不依赖内存变量；
- 重复 helper 被本地文件锁隔离；
- 提交不确定和回滚中断具有单一恢复方向；
- updater 上线前具备最小无人值守恢复基础。

成本：

- updater 容器不再依靠 `AutoRemove` 自动清理；
- 结果协议需要 schema v2 和旧 schema 兼容读取；
- Appliance 需要清理已退出且身份匹配的终态协调器；
- `/data` 必须提供可靠的本地 `flock` 语义；
- 状态机每个副作用都需要检查点与故障注入测试。

## 已考虑的备选方案

### 保留 AutoRemove，由 Appliance 重新创建 helper

旧容器已停止且候选尚未创建时不存在活动 Appliance，无法保证有人重新创建，因此
拒绝。

### 使用 SQLite 作为协调器锁

候选迁移、快照恢复和旧版本启动期间数据库内容会被替换，不适合作为跨恢复阶段的
稳定锁；同时会扩大 updater 的数据库权限，因此拒绝。

### 使用 PID 文件或时间租约

PID 会在容器重启后复用，时间租约会引入过期与 stale writer 问题。单 NAS 本地文件
锁能随进程退出由内核释放，因此拒绝 PID 和纯时间租约。

### 所有提交前中断都继续升级

候选稳定性和执行位置可能已经失去可信观察。提交前允许回滚，选择单向回到旧版本更
保守，因此拒绝。

## 未来复审

出现以下情况时应复审本决策：

- MediaSync 支持网络文件系统上的 `/data`；
- 引入 PostgreSQL 或外部事务协调器；
- Docker 提供原子容器替换与持久恢复事务；
- 真实 NAS 故障演练证明 `flock` 或协调器 restart policy 不可靠。
