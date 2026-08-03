# Updater Docker 与 NAS 故障恢复演练

本文档用于执行 Issue #128 的生产入口前验收。它只验证恢复契约，不开放安装 API，
也不代表一键更新功能已经可以面向普通用户启用。

## 1. 验收层次

| 层次 | 验证内容 | 执行位置 | 是否自动化 |
| --- | --- | --- | --- |
| 状态机故障注入 | 每个副作用调用前、成功后、检查点前后均可恢复 | 后端测试 | 是 |
| Docker 运行时契约 | `flock`、helper 重启、锁竞争、重启策略解除 | Docker 主机 | 是 |
| NAS 完整演练 | Docker daemon/NAS 重启、真实升级、真实回滚 | 群晖、飞牛 | 人工执行并留证 |

三层都通过后才能关闭 Issue #128。只通过 CI 或本机 Docker 不得视为 NAS 验收完成。

## 2. 自动故障矩阵

仓库测试 `backend/tests/test_updater_forward_v2.py` 覆盖：

- 正常前向路径 45 个中断窗口；
- 自动回滚路径 44 个中断窗口；
- 每次中断后重建执行器并验证副作用不会重复；
- fault hook 与参数化测试矩阵的一致性守卫。

执行：

```bash
cd backend
.venv/bin/pytest -q tests/test_updater_forward_v2.py
```

## 3. Docker 运行时契约演练

先通过 NAS 镜像管理器拉取要验收的精确镜像。脚本不会自动拉取镜像，也不会操作
现有 MediaSync 容器。它只创建带唯一时间戳的临时容器和临时数据卷，退出时自动清理。

```bash
mkdir -p /volume1/docker/mediasync/rehearsals
./scripts/updater_recovery_rehearsal.sh \
  josephyjq/mediasync@sha256:<目标摘要> \
  synology \
  /volume1/docker/mediasync/rehearsals
```

飞牛示例：

```bash
mkdir -p /vol1/1000/docker/mediasync/rehearsals
./scripts/updater_recovery_rehearsal.sh \
  josephyjq/mediasync@sha256:<目标摘要> \
  fnos \
  /vol1/1000/docker/mediasync/rehearsals
```

普通 Docker 主机把平台参数改为 `docker`。报告为 JSON，至少包含镜像 ID、RepoDigest、
Docker Engine 版本、UTC 完成时间、helper 重启次数和五项布尔验收结果。

脚本验证：

1. 第一个 helper 独占 `/data/update/updater.lock`；
2. 并发 helper 因 `flock` 冲突退出，且不获取执行权；
3. helper 异常退出后被 `unless-stopped` 恢复；
4. 重启后的 helper 重新获得同一把锁；
5. 终态前把 restart policy 改为 `no` 并确认；
6. helper 停止后锁可以由后续进程安全取得。

## 4. 群晖与飞牛真实演练矩阵

每个平台都必须使用测试实例和完整 `/data` 备份，不得直接在唯一生产实例上做故障注入。

| 场景 | 预期结果 | 群晖 | 飞牛 |
| --- | --- | --- | --- |
| helper 执行时重启 Docker daemon | 原 helper 或新一代 helper 唯一接管 | 待执行 | 待执行 |
| 旧容器停止后重启 NAS | 进入同一次自动回滚，不创建第二候选 | 待执行 | 待执行 |
| 候选创建后重启 NAS | 识别唯一候选并继续回滚 | 待执行 | 待执行 |
| `COMMIT_REQUESTED` 后重启 NAS | 只保持/启动候选，不恢复旧快照 | 待执行 | 待执行 |
| 回滚恢复快照后重启 NAS | 继续恢复旧容器，不重复恢复出多实例 | 待执行 | 待执行 |
| 两个 helper 同时启动 | 只有一个获得锁，另一个无 Docker 副作用 | 待执行 | 待执行 |
| 成功或人工终态 | helper restart policy 为 `no`，随后可清理 | 待执行 | 待执行 |

每一格完成后，应在 Issue #128 附上：

- NAS 型号、CPU 架构、系统版本和 Docker Engine 版本；
- 源镜像与目标镜像的精确 digest；
- 注入故障的 UTC 时间和检查点；
- 重启前后 `docker ps -a`、updater 结果 JSON 与应用健康结果；
- 是否同时出现新旧两个运行中 Appliance；
- 自动收敛结果或实际执行的人工恢复步骤。

## 5. 失败关闭与人工恢复

出现以下任一情况立即停止自动演练、保留 `/data` 和全部容器现场，并创建独立修复
Issue：

- 新旧 Appliance 同时运行；
- 多个 helper 同时持有执行权；
- `COMMIT_REQUESTED` 后旧快照被恢复；
- updater 结果、pending、候选 token 或 Docker 身份互相冲突；
- recovery generation 超过 3；
- helper 进入终态后仍保持自动重启。

人工恢复必须使用演练前完整 `/data` 备份和精确源镜像。不得只恢复 SQLite，也不得在
未确认云盘远端副作用前重复执行转存任务。
