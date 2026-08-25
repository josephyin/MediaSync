# 夸克网盘单项转存验收

该命令只用于维护者在本机验证 Cookie 私有接口的可恢复写入链路。它只接受一个
顶层项目的分享，目标目录默认为 `/MediaSync测试`，写前拒绝同名覆盖；Cookie 和
分享密码只在当前进程内使用，不写入状态文件或输出。

测试分享必须由另一个夸克账号创建。目标账号自己创建的分享虽然可以读取，但实测
保存会返回 `HTTP 404 / code 41017`。

```bash
cd backend
.venv/bin/python -m app.providers.quark.write_cli --cookie-clipboard
```

按提示输入跨账号单项分享 URL、可选密码，并准确输入确认文本
`WRITE ONE TEST ITEM`。成功报告必须同时满足：

- `remote_write_performed` 为 `true`；
- `submitted` 和 `completed` 为 `true`；
- `target_verified` 为 `true`；
- `credentials_persisted` 为 `false`。

若远端已返回 operation ID 但尚未完成，使用相同 Cookie 和分享 URL 重跑同一命令，
程序只恢复查询，不再次提交保存请求。若提示存在只有 intent 的不确定状态，停止操作
并人工核对目标目录，不能删除状态后盲目重试。

探针成功后，测试文件会留在目标目录中，由维护者确认无用后在夸克网盘界面手工删除。
