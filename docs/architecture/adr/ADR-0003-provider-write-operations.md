# ADR-0003：可恢复的 Provider 写操作

- 状态：Accepted，夸克真实写入验收通过
- 日期：2026-08-25

## 决策

异步云盘写入必须把远端 operation ID 持久化在 `tasks`，不能只存在一次 Worker
运行的内存中。`tasks` 增加：

- `provider_write_intent_at`：首次准备提交写请求的时间；
- `provider_operation_id`：远端返回的稳定任务 ID；
- `provider_operation_status`：`intent`、`pending`、`succeeded` 或 `uncertain`；
- `provider_result`：只保存目标文件 ID、目标路径等脱敏结果。

Worker 的顺序固定为：目标存在性检查、提交本地 intent、提交一次远端写请求、持久化
operation ID、查询远端任务、核对目标结果。已有 operation ID 的重试只能继续查询，
不得再次提交写请求。

## 不确定结果

POST 超时、连接中断、5xx、成功响应缺少 operation ID，或者远端接受后本地无法保存
operation ID，都标记为 `uncertain`。该状态终止自动重试并要求人工对账，不能通过
“目标暂时还没出现”推断远端没有接受请求。

## 夸克边界

夸克 Cookie Provider 的分享保存使用远端 `task_id`，完成后只接受恰好一个
`save_as_top_fids` 作为单文件转存结果。多结果、空结果或结构变化进入待对账，不能
按数组顺序或文件名猜测。

目录创建和分享保存 POST 均不做 HTTP 级自动重放。Cookie 只在内存中轮换，并沿用
现有加密持久化路径；operation 记录不得包含 Cookie、`stoken` 或
`share_fid_token`。

## 启用门禁

离线测试通过仍不声明 `folder_create` 或 `share_save`。必须先用维护者自有测试目录和
单项测试分享完成一次脱敏真实验收，确认创建、提交、pending、完成、目标 ID 和 Cookie
轮换行为后，才能修改 Provider 能力元数据并允许创建夸克订阅。

2026-08-25，维护者使用目标账号 Cookie 和另一账号创建的单项分享完成真实验收：
提交成功、远端任务完成、返回目标 ID、目标目录复核成功，并观察到 Cookie 只在内存
轮换。相同目标账号自己创建的分享会在保存阶段返回 `404/41017`，因此该情形作为
已知限制，不视为凭证失效，也不得自动重试。夸克能力据此标记为 `experimental`。
