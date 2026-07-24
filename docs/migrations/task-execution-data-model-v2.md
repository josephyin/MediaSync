# Task Execution 数据模型 v2 迁移

本迁移把 v0.1 SQLite 数据库从 `0005_folder_checkpoints` 升级到
`0006_task_execution_data_model_v2`。

## 升级前

1. 停止 MediaSync，确保 Scheduler 和转存任务不再写入 SQLite。
2. 将 SQLite 数据库及其 `-wal`、`-shm` 文件作为一个一致性备份整体复制，
   或使用 SQLite 备份命令。
3. 记录当前应用镜像和 Alembic 版本。
4. 执行：

```text
alembic upgrade head
```

## 保留的数据

迁移会保留现有账号、订阅、文件和任务。v0.1 Task 的兼容字段也会保留，使当前
单进程运行时仍能继续工作。

现有 Task 将获得：

- `payload_version = 1`；
- 空 JSON Payload；
- 确定性的重试和完成兼容值；
- 在关联订阅可用时，从订阅推导出的账号引用。

存在执行证据的 Task 会获得一条合成 Task Run，用于记录最近一次 v0.1 结果。
由于 v0.1 没有保留每次独立尝试，迁移无法重建此前已被覆盖的历史尝试。

SQLite 数据保护会使 Task Payload/版本、已经赋值的幂等键以及 Task Run 标识
不可变。终态 Task Run 不可更新，正常数据库操作也不能删除 Task Run 历史。

## 回滚与恢复

执行 `alembic downgrade 0005_folder_checkpoints` 会删除 `task_runs` 以及所有
Task Engine v2 字段。升级后产生的 Task Run 历史会在降级时丢失。

生产环境回滚应恢复迁移前的 SQLite 备份，并配合此前的应用镜像。不得让旧版
应用镜像直接访问仍处于 `0006_task_execution_data_model_v2` 的数据库。

## 范围

本迁移只保存后续 Task Repository 所需字段，不实现任务领取、心跳、租约过期、
恢复、Worker 进程或 Scheduler 变更。
