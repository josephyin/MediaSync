# 夸克 OpenList OpenAPI 只读验收

该命令只验证 OpenList 刷新、夸克 OpenAPI 账号和根目录第一页，不保存凭证，也不输出
账号身份、用户 ID、文件名或文件 ID。

## 准备

从 OpenList 夸克 OAuth 授权结果准备：

- Refresh Token
- AppID
- SignKey

OpenList 授权页上的“使用 OpenList 提供的参数”只用于完成 OAuth 授权，不会在
`/quarkyun/renewapi` 响应中返回 AppID 或 SignKey。夸克 OpenAPI 的每次业务请求仍要求
`x-pan-client-id`，并要求使用 SignKey 生成 `x-pan-token`，因此 MediaSync 直连模式下
三项均为必填。没有与 token 配套的 AppID、SignKey 时，不能执行此项验收。

不要把这些值粘贴到聊天、Issue、日志或命令参数中。

## 执行

在项目根目录执行：

```bash
cd backend
.venv/bin/python -m app.providers.quark.open_cli
```

程序依次隐藏询问三项必填材料。默认刷新节点为：

```text
https://api.oplist.org/quarkyun/renewapi
```

如果 token 来自另一个可信 OpenList/APIPages 节点，必须使用同一节点：

```bash
.venv/bin/python -m app.providers.quark.open_cli \
  --token-url https://your-trusted-host.example/quarkyun/renewapi
```

Token URL 必须是 HTTPS，不能包含用户名、密码、查询参数或 fragment。OpenList 当前
刷新协议会把 Refresh Token 放在 GET 查询参数中；MediaSync 不打印完整请求 URL，
但所选 broker、反向代理和网络边界仍可能看到该值，只能使用你信任的节点。

## 通过标准

成功报告应满足：

```json
{
  "provider": "quark_open",
  "mode": "openlist_open_api",
  "persisted": false,
  "checks": {
    "account_accepted": true,
    "default_drive_id_present": true,
    "root_item_count": 0,
    "root_has_more": false,
    "rotated_refresh_token": true
  }
}
```

`root_item_count`、`root_has_more` 和 `rotated_refresh_token` 可因账号内容和 broker 行为
不同而变化，不是固定期望值。硬门禁只有：命令退出码为 0、`account_accepted` 和
`default_drive_id_present` 为 `true`、`persisted` 为 `false`。

完成后只粘贴这份脱敏 JSON，不要粘贴终端输入、授权页面结果或 URL 查询参数。
