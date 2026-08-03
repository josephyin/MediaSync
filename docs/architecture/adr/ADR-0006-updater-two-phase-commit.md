# ADR-0006：Updater 两阶段提交与重启策略 Fencing

- 状态：已接受
- 日期：2026-08-03
- 决策者：MediaSync 维护者
- 取代：无
- 被取代：无
- 相关内容：[Docker Socket 一键镜像升级设计](../docker-socket-one-click-update.md)、ADR-0005、Issue #107

## 背景

ADR-0005 接受了可选 Docker Socket 与临时 updater 助手，但没有定义候选验证通过后，
文件结果、Appliance 数据库事务和旧容器删除之间的精确提交点。

早期设计要求 updater 先写入不可变 `SUCCESS`，再等待 Appliance 把数据库操作推进到
成功。如果等待超时，updater 无法判断数据库事务是尚未执行、正在执行，还是已经提交
但标记清理尚未完成。此时恢复旧快照可能覆盖已经提交的数据，继续报告 `SUCCESS` 又
可能掩盖未提交状态。

另一个独立风险是旧容器保留 `always` 或 `unless-stopped` restart policy。切换期间
若 NAS 或 Docker daemon 重启，Docker 可能同时拉起新旧两个 MediaSync 实例。

## 决策

Updater 的成功路径采用两阶段握手：

```text
VERIFYING
    ↓
COMMIT_REQUESTED
    ↓
Appliance 数据库事务提交 SUCCESS 并清理运行标记
    ↓
Updater 确认提交
    ↓
SUCCESS
```

`COMMIT_REQUESTED` 只属于 updater 结果文件，不写入数据库
`update_operations.status`。Appliance 独占数据库业务状态写入，验证提交请求后以单个
事务执行 `VERIFYING -> SUCCESS` 并释放 `active_slot`。

Updater 只有观察到数据库终态、活动槽释放以及 pending、candidate evidence、handoff
全部清理后，才能把结果文件推进到 `SUCCESS` 并删除旧容器。

停止旧容器前，Updater 必须记录其原始 restart policy，并把旧容器策略临时更新为
`no`。候选容器使用 handoff 中记录的原始策略。只有进入回滚路径、候选停止且旧快照
恢复完成后，才能恢复旧容器原策略并启动旧容器。

## 不变量

- `SUCCESS` 只能表示数据库终态和运行标记清理已经得到确认。
- `COMMIT_REQUESTED` 是非终态但不可回滚的提交不确定状态。
- 不存在 `COMMIT_REQUESTED -> ROLLING_BACK` 转换。
- 确认超时不得启动旧容器、恢复旧快照或删除候选容器。
- 提交不确定时必须保持候选运行、旧容器停止并保留恢复现场。
- restart policy fencing 失败时不得停止旧容器。
- 旧容器与候选容器不得在任何自动恢复路径中同时运行。
- Updater 或恢复协调器重启后必须依据数据库终态与运行标记收敛，不能猜测提交结果。

## 后果

收益：

- 数据库提交与 updater 进程退出之间不存在猜测性回滚；
- `SUCCESS` 具有稳定、可审计的完成语义；
- NAS 重启不会因为旧 restart policy 自动形成双实例；
- Updater 崩溃后可以从 `COMMIT_REQUESTED` 安全恢复确认。

成本：

- updater 结果协议新增一个持久化状态；
- Appliance 对账器需要区分提交请求与最终确认；
- Docker 生命周期客户端需要受限的 restart policy 更新操作；
- 自动回滚只能覆盖提交请求之前的故障；
- 上线前必须实现提交不确定状态的恢复协调器。

## 已考虑的备选方案

### 继续先写 SUCCESS

实现简单，但 `SUCCESS` 无法证明数据库事务已经提交，确认超时也没有安全恢复方向，
因此拒绝。

### COMMIT_REQUESTED 超时后自动回滚

数据库事务可能已经提交。恢复旧快照会破坏已提交数据，并可能同时启动新旧容器，
因此拒绝。

### 让 updater 直接写 SQLite

可以减少握手步骤，但会让 updater 与候选 Appliance 同时拥有数据库状态机写权限，
违反进程边界并增加 SQLite 竞争，因此拒绝。

### 保留旧容器 restart policy

正常路径更少一次 Docker 调用，但 NAS 重启可能形成双实例，因此拒绝。

## 未来复审

出现以下情况时应复审本决策：

- Docker 提供原子的容器替换与单实例保证；
- MediaSync 改用支持事务队列的外部数据库；
- NAS 平台能提供原生、可恢复的应用升级事务；
- 真实故障演练证明 restart policy fencing 在目标平台不可用。
