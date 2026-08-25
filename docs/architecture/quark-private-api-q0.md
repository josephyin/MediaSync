# 夸克私有接口 Q0 验证记录

- 日期：2026-08-25
- 结论：Q2 真实只读及 Q6 单项跨账号转存验证已于 2026-08-25 通过
- 路线：实验性 Web 私有接口；在双凭证模式中负责分享链路
- 对 rc.17 的影响：无

## 1. 验证范围与证据

本轮没有使用真实账号、浏览器会话或 Cookie，也没有向夸克业务接口发请求。结论
来自现有 MediaSync 契约审计，以及两个仍在维护的第三方实现的固定提交：

- [OpenList Quark 驱动，提交 1a6cabf](https://github.com/OpenListTeam/OpenList/tree/1a6cabf45aecf66c6d2ff6c32aed39d50264f43c/drivers/quark_uc)
- [quark-auto-save，提交 1c35e96](https://github.com/Cp0204/quark-auto-save/blob/1c35e969332e03980f60c34fc15b2467370bafb2/quark_auto_save.py)

两份参考实现均为 AGPL-3.0，MediaSync 为 MIT。后续只能依据已验证的协议行为做
独立实现，不复制或改写参考实现的代码、类型组织、控制流或测试夹具。接口事实也
必须由 MediaSync 自有测试记录复核，不能把第三方实现当成官方文档。

## 2. 静态能力结论

| MediaSync 能力 | 静态结论 | 观察到的协议行为 | 进入运行时前的门禁 |
|---|---|---|---|
| 账号验证 | 可实现 | Cookie 会话访问账号信息或固定配置端点 | 自有账号确认稳定身份字段和失效错误 |
| 目标目录浏览 | 可实现 | `file/sort` 使用父目录 `fid` 和页码分页 | 验证空页、总数变化和风险文件名行为 |
| 分享解析 | 可实现 | 分享 ID 与密码换取短期 `stoken` | 验证错误密码、失效分享和访问限制 |
| 分享目录浏览 | 可实现 | `sharepage/detail` 返回 `fid`、父目录和 `share_fid_token` | 验证嵌套目录、分页和稳定字段 |
| 目录创建 | 可实现 | 创建接口返回目标目录 `fid` | 验证同名冲突不会产生重复目录 |
| 分享转存 | 条件可实现 | 保存接口返回 `task_id`，任务完成后返回目标 `fid` 列表 | 必须先完成持久化 task 对账设计和真实账号验证 |
| 凭证轮换 | 可实现 | 响应可能通过 `Set-Cookie` 轮换 `__puus` | 验证合并规则、持久化原子性和过期行为 |

这只能证明协议形状足以设计 Provider，不能证明 2026-08-24 的线上接口对任意账号
稳定可用。

## 3. 认证契约

### 3.1 凭证形态

实验性 Provider 使用完整 Cookie 会话，而不是 OAuth refresh token。实现必须：

- 只接受 Cookie 键值串，不接受浏览器导出的数据库、Profile 或任意请求头集合；
- 拒绝控制字符、换行、重复关键键和超出上限的输入；
- 加密后保存，API 响应、日志、Task payload 和异常中不得出现任何 Cookie 值；
- 只向代码内固定的 `https://pan.quark.cn`、`https://drive.quark.cn` 或经验证的
  夸克子域发送，用户输入不得决定 API host；
- 分享 URL 只接受 `https://pan.quark.cn/s/<share-id>` 形态并本地解析，禁止跟随
  分享 URL 向任意主机发请求。

### 3.2 数据模型决定

不把 Cookie 伪装成 `refresh_token`。Q2 前提出一次通用凭证迁移：

- 将 `cloud_accounts.refresh_token` 迁移为加密的 `credential_payload`；
- 新增 `credential_type`，现有阿里云盘数据迁移为 `refresh_token`，夸克为
  `cookie`；
- 账号创建/更新请求按 Provider 校验凭证类型；
- 完成切换后删除旧字段和旧请求名，不保留长期双写或隐式兼容分支；
- 迁移必须覆盖 rc.17 数据库升级、全新数据库和回滚前备份说明。

这是同一张 `cloud_accounts` 表内的加法与重命名，不改变四张核心业务表骨架。

### 3.3 Cookie 轮换

HTTP 客户端可以从响应 `Set-Cookie` 中提取允许轮换的会话键并合并到内存凭证，
但只有后续账号验证成功后才把完整新凭证原子写回数据库。验证失败时保留原密文，
账号进入 `expired` 或 `error`，不循环刷新。

首版只允许明确列入白名单的会话键轮换，不接受响应设置任意键。确切白名单必须由
自有账号实时验证确定，静态参考中的名称不能直接视为长期契约。

## 4. 分页和 DTO 映射

内部客户端可以使用页码访问上游，但 Provider 必须继续返回通用
`RemotePage.next_marker`：

- marker 采用 MediaSync 自有不透明编码，至少绑定 Provider、目录 ID 和下一页；
- marker 不包含 Cookie、`stoken` 或 `share_fid_token`；
- 上游 `fid` 映射为 `remote_file_id`，父 `fid` 映射为 `parent_id`；
- 目录/文件标志映射为稳定的 `item_type`，上游原始类别只放入 `metadata`；
- HTML 实体、空名称、非法时间和超大数值必须在 Provider 边界规范化或拒绝。

扫描层不得感知页码、`stoken` 或夸克响应结构。

## 5. 转存和持久化对账

静态证据显示转存是异步操作：保存请求先返回 `task_id`，任务完成后才返回一个或
多个目标 `fid`。这对 MediaSync 有一个现存契约缺口：`save_shared_item` 只返回最终
`SaveResult`，当前 Task/TaskRun 也没有结构化保存 Provider operation ID。

Q4 前必须先完成通用设计：

1. 发出保存请求前，先在数据库提交带时间戳的 `write_intent`；
2. 保存请求成功取得 `task_id` 后，必须在继续轮询前持久化；
3. Worker 丢失租约或进程重启后，新的执行者先恢复查询该 `task_id`，不得再次提交
   保存请求；
4. 上游任务完成后，记录返回的目标 `fid`，并通过目标目录列表再次核对；
5. 轮询超时、网络中断或返回结构异常时进入“待对账”，不标记失败后直接重试写入；
6. 如果进程在上游接受请求后、持久化 `task_id` 前崩溃，恢复时会看到只有
   `write_intent` 的不确定状态；此时只能在冷却期内查询目标或交由人工处置，不能
   自动再次提交；
7. 只有明确证明上游未接受原请求，或经批准的对账规则证明目标不存在，才允许新的
   保存请求；
8. 多文件返回结果必须验证数量和身份映射，不能只按名称或数组顺序盲目认领。

推荐在 `tasks` 增加通用的 `provider_write_intent_at`、`provider_operation_id`、
`provider_operation_status` 和脱敏的 `provider_result`，让 operation 状态跨
TaskRun 和 Worker 租约延续，而不是新建夸克专用表。具体不可变性、恢复、冷却期和
终态约束需要在 Q4 ADR 中单独评审。

该通用缺口已由迁移 `0008_provider_operations` 和 ADR-0003 的可恢复写契约关闭。
维护者随后完成单项跨账号真实转存及目标 ID 对账，现已声明 `share_save`；能力仍
标记为实验性。

## 6. 错误和重试边界

实现不能只按 HTTP 状态判断；上游可能在 JSON 中返回独立的 `status`、`code` 和
`message`。MediaSync 需要自己的稳定错误分类：

- `QUARK_AUTH_EXPIRED`：Cookie 失效或账号未登录；
- `QUARK_SHARE_INVALID`：分享失效、密码错误或无权访问；
- `QUARK_RATE_LIMITED`：明确限流；
- `QUARK_RISK_CONTROL`：验证码、风控或设备限制；
- `QUARK_OPERATION_PENDING`：已有 task 尚未形成确定结果；
- `QUARK_UPSTREAM_CHANGED`：成功/错误结构不符合已验证契约。

读取请求只对网络错误、明确限流和可恢复服务错误做有界重试。认证、分享权限、风控
和协议变更不重试。任何写请求均不得由通用 HTTP retry 自动重放。

## 7. Q0 结论和下一门禁

Q0 静态验证已经批准并完成 Q1：前端已改为按 Provider 元数据渲染；Q1 阶段当时保持
夸克禁用。Q1 没有增加夸克网络客户端、没有接收 Cookie、没有修改数据库，也没有
改变阿里云盘 Provider 运行行为。前端测试、生产构建和本地页面检查均通过。

进入 Q2 前还需要：

1. 通过 MediaSync 自有诊断程序，让维护者在本地输入 Cookie；程序只能输出脱敏的
   能力结果，不能保存或打印凭证；
2. 对账号验证、一个空目录、一个分页目录和一个自有测试分享做只读验证；
3. 记录上游状态码/业务码类别和响应字段形状，但不保存真实文件名、账号信息或
   Cookie；
4. 根据实测结果确定 Cookie 轮换白名单、超时和限流预算。

真实转存测试推迟到 Q4 对账设计完成之后，避免产生无法恢复或重复的写操作。

## 8. Q2 诊断实现状态

2026-08-25 已完成独立诊断和正式只读适配：

- `backend/app/providers/quark/readonly_probe.py`：固定域名、Cookie 校验、内存轮换、
  账号/根目录/分享第一页结构检查和稳定错误分类；
- `backend/app/providers/quark/cli.py`：通过终端隐藏输入 Cookie 与分享密码，只输出
  脱敏 JSON；
- `backend/app/providers/quark/provider.py`：映射通用账号、分享、目录、分页和文件
  DTO，并实现目录创建、可恢复分享转存和远端任务查询；
- `backend/tests/test_quark_readonly_probe.py`：覆盖输入注入、固定 host、轮换白名单、
  结构化输出、凭证不回显和错误分类。
- `backend/tests/test_quark_provider.py`：覆盖正式契约、分页、加密轮换、有限重试、
  可恢复写操作和能力注册边界。

该模块已经以实验性能力加入 `backend/app/providers/registry.py`，Cookie 沿用现有加密
凭证字段。只读操作步骤见
[夸克网盘只读诊断手册](../operations/quark-readonly-probe.md)。

维护者已用自有账号完成账号、根目录和测试分享的脱敏实时检查，三项均通过。Q2 至此
完成。维护者随后确认采用双凭证模式；OpenAPI 只负责账号盘能力，私有 Provider
保留分享职责。

## 9. Q6 真实写入验收

2026-08-25，维护者使用目标账号 Cookie 对另一账号创建的单项分享执行脱敏写入探针，
结果确认 `submitted`、`completed`、`target_verified` 和内存 Cookie 轮换均为真，且
凭证没有持久化到探针状态。

同一目标账号自己创建的分享可读取，但保存请求返回 `HTTP 404 / code 41017`；换成
另一账号的分享后成功。因此当前把“不能把本账号自己的分享转存回同一账号”记录为
实测限制。遇到 `41017` 时停止重试并提示改用其他账号创建的分享。

夸克私有 Provider 据此开放 `folder_create` 和 `share_save` 元数据，状态保持
`experimental`。这不改变 rc.17，也不进入 v0.2.0 正式版稳定性观察基线。
