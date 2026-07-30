# v0.2 单容器备份恢复演练记录

- 日期：2026-07-30
- 依据：Issue #73、ADR-0004、Issue #34
- 被测提交：`8172fff1f9d9794fb4b257d5cabec0964879ac4f`
- 部署模式：单容器 Appliance
- 数据源：两个相互独立的 Docker named volume

## 1. 目标

验证 MediaSync 在正常停止后备份完整 `/data`，可以恢复到全新数据卷，并同时
保留：

- SQLite 数据和迁移版本；
- 云盘账号业务记录；
- 凭证加密密钥；
- 管理员密码和会话修订号；
- 运行时密钥文件权限；
- API、Scheduler、Worker、Nginx 和 Launcher 健康状态。

本次演练不验证运行中热拷贝。v0.2 面向普通 NAS 用户的权威流程仍是先停止容器，
再备份完整宿主机数据目录。

## 2. 隔离资源

演练使用下列临时资源：

```text
源容器：mediasync-backup-source-73
源数据卷：mediasync-backup-source-73
恢复容器：mediasync-backup-restore-73
恢复数据卷：mediasync-backup-restore-73
临时归档：mediasync-backup-73.tar.gz
```

源容器和恢复容器没有同时访问同一个数据卷。恢复数据卷在解压前为空，避免把
“原卷重新挂载”误当成恢复成功。

## 3. 准备可识别状态

源容器从全新数据卷启动并进入 `healthy` 后：

1. 使用初始管理员账号登录；
2. 创建名为 `backup-restore-marker-73` 的测试云盘账号记录；
3. 在线修改管理员密码；
4. 验证新密码可以登录；
5. 确认 `admin_session_revision` 从 `0` 变为 `1`。

测试 refresh token 和测试密码只用于隔离容器，没有写入文档。后续日志扫描也
没有发现这些值。

## 4. 备份

先正常停止源容器，让 API、Scheduler、Worker 和 SQLite 完成关闭：

```bash
docker stop --time 120 mediasync
```

然后归档整个数据目录，而不是只复制 `mediasync.db`：

```bash
tar -C /data -czf mediasync-backup.tar.gz .
```

本次临时归档的 SHA-256：

```text
731c8e34ecb36e2cbdc0c129d7a73900d3cd6e4a7e96509805220deaa077f0dc
```

归档包含 SQLite 文件和 `/data/config/runtime-secrets.json`。二者属于同一恢复
单元，任何一个缺失都不能视为有效备份。

## 5. 恢复

恢复步骤：

1. 创建全新空数据卷；
2. 把归档完整解压到新卷根目录；
3. 使用与备份对应的同一 MediaSync 镜像启动恢复容器；
4. 等待容器健康；
5. 执行登录、业务记录、SQLite 和运行时密钥验证。

NAS 上应把“新数据卷”替换为新的宿主机测试目录。第一次演练不要覆盖生产目录，
也不要让生产容器和恢复容器同时挂载同一个 `/data`。

## 6. 验证结果

| 验证项 | 结果 |
|---|---|
| 恢复容器健康 | 通过 |
| Launcher、Nginx、API、Scheduler、Worker | 全部通过 |
| 初始旧密码登录 | HTTP `401` |
| 备份前设置的新密码登录 | HTTP `200` |
| 业务标记记录数量 | `1` |
| `PRAGMA integrity_check` | `ok` |
| `admin_session_revision` | `1` |
| `/data/config` 权限 | `0700` |
| `runtime-secrets.json` 权限 | `0600` |
| 源卷与恢复卷运行时密钥文件 SHA-256 | 一致 |
| 恢复容器日志敏感值扫描 | 未发现 |

验证摘要：

```text
old_login=401
new_login=200
marker_count=1
integrity=ok
revision=1
config_mode=700
secret_mode=600
secrets_match=true
logs=clean
```

## 7. 结论

当前 v0.2 单容器 Appliance 的停机备份与恢复契约通过演练：

- 完整 `/data` 可以恢复 SQLite 业务数据和运行时密钥；
- 在线修改后的管理员密码与会话修订号随备份恢复；
- 默认密码不会覆盖恢复数据中的管理员密码；
- 恢复后 SQLite 完整，五个关键组件健康；
- 文件权限和日志脱敏符合设计要求。

## 8. 限制和后续观察

- 本次只验证正常停机备份，不声明运行中直接复制 SQLite 文件是安全的。
- 本次使用当前提交构建的本地镜像；正式版评审时应使用候选发布的精确镜像标签
  再复验一次。
- 恢复 SQLite 不会撤销备份后已经发生的云盘远端操作。生产回滚后触发重试前，
  仍需先进行远端对账。
- Issue #34 观察期内应至少在实际 NAS 宿主机目录上再完成一次同类恢复，并记录
  NAS 型号、文件系统、归档位置、耗时和恢复结果。

