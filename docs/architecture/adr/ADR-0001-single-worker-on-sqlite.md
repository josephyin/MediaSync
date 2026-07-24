# ADR-0001：SQLite 单 Worker

- 状态：已接受
- 日期：2026-07-23
- 决策者：MediaSync 维护者
- 取代：无
- 被取代：无
- 相关内容：[Worker 与 Task Engine v2 架构](../worker-task-engine-v2.md)

## 背景

MediaSync 主要由家庭用户部署在 NAS 上。近期工作负载预计为每天数十到数百次
同步操作。在这一场景中，可靠性、简单的故障恢复和较低的运维成本，比横向吞吐
能力更重要。

在 SQLite 上支持并发 Worker 会增加锁竞争、部署歧义和 v0.2 暂时不需要处理的
恢复场景。在默认安装中加入 Redis、消息队列或 PostgreSQL，也会增加目标 NAS
用户的使用负担。

## 决策

v0.2 默认且受支持的部署方案为：

```text
SQLite WAL
+ 一个 mediasync-worker 进程
+ 任务并发数 1
```

API 和 Scheduler 进程可以使用同一个 SQLite 数据库，但只能有一个 Worker 进程
消费任务。运维人员不得在 SQLite 部署中横向扩展 Worker 服务。

Task Engine 仍然使用原子领取、租约、心跳和基于 `lock_token` 的 fencing。单
Worker 限制只是简化受支持的部署拓扑，并不意味着可以放宽安全的所有权规则。

## 不变量

- 一个 SQLite 数据库最多只支持一个 Worker 进程。
- Worker 任务并发数为 `1`。
- 调用 Provider 网络接口时不得持有 SQLite 写事务。
- 任务领取和状态转换事务必须保持短小。
- Docker Compose 只声明一个 Worker 副本。
- 项目不得把 SQLite 多 Worker 描述为受支持或经过测试的方案。

## 后果

优点：

- 默认部署保持轻量并适合 NAS；
- 队列持久化无需外部服务；
- 故障诊断、备份和恢复容易理解；
- v0.2 的工程投入可以专注正确性，而不是吞吐量。

限制：

- 长任务只能串行处理；
- 一个缓慢的 Provider 操作可能延迟后续任务；
- SQLite 不能作为受支持的多 Worker 方案；
- 横向提升任务吞吐量需要未来新增部署方案。

## 已考虑的备选方案

### 默认使用 Redis、RabbitMQ 或 Celery

拒绝。它们会增加服务数量、持久化语义、配置和故障模式，目前没有证据表明 NAS
工作负载需要这些组件。

### 在 SQLite 上运行多个 Worker

v0.2 暂不采用。安全的任务领取机制本身不能消除 SQLite 写竞争，也不能提供清晰
且可支持的运维模型。

### 默认使用 PostgreSQL

拒绝。对不需要多 Worker 吞吐量的用户来说，这会提高最低部署与维护成本。

## 未来复审

只有满足以下条件后，PostgreSQL 多 Worker 方案才可以补充或取代本决策：

- 真实工作负载证明存在吞吐量需求；
- 任务领取、租约、恢复和 fencing 已经过并发测试；
- 迁移、备份和部署操作已有完整文档；
- 设计 PR 和新 ADR 明确定义受支持的部署方案。

除非后续已接受的 ADR 明确修改，SQLite 始终是 NAS 优先的默认方案。
