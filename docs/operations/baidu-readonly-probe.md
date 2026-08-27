# 百度网盘 OpenAPI 只读探针

本探针只验证百度网盘官方 OpenAPI 的账号和根目录读取能力，不保存或打印凭证，也不
执行建目录、上传、分享转存等写操作。

## 准备 Access Token

打开 OpenList Token 获取工具，选择“百度网盘 验证登录”，完成授权后会同时显示：

- Access Token（访问令牌）
- Refresh Token（刷新令牌）

本轮只复制 **Access Token**。不要复制 Refresh Token，也不要把任何令牌发到聊天、
工单或日志中。只读探针刻意不刷新令牌，避免刷新后产生的新 Refresh Token 未被保存。

## macOS 本机执行

在项目根目录进入后端：

```bash
cd backend
```

把 Access Token 复制到剪贴板，然后执行：

```bash
uv run --frozen python -m app.providers.baidu.cli --token-clipboard
```

也可以不使用剪贴板参数，随后在隐藏输入提示中粘贴：

```bash
uv run --frozen python -m app.providers.baidu.cli
```

成功报告应满足：

- `provider` 为 `baidu`；
- `mode` 为 `official_open_api`；
- `persisted` 为 `false`；
- `checks.account.session_accepted` 为 `true`；
- `checks.root` 只包含数量和字段名，不包含文件名、路径、用户信息或令牌。

这一结果只证明账号盘的官方 OpenAPI 读取能力。百度公开分享的读取和转存需要独立的
Web 会话探针，不能由本结果推断为可用。

## 第二阶段：Web 分享只读探针

登录百度网盘 Web 端后，在浏览器开发者工具的 Network 中复制发往
`pan.baidu.com` 请求的完整 `Cookie` 请求头。Cookie 必须包含非空的 `BDUSS`；不要
把 Cookie 发到聊天、工单或日志中。

也可以在浏览器的 Cookie 表格中只复制 `BDUSS` 对应的 Value；探针会在内存中将该
单独值（包括自身带 `=` 填充的格式）识别为 `BDUSS`，无需手工拼接 `BDUSS=`。
复制完整请求头时，是否包含开头的 `Cookie:` 字段名都可以。

探针只从输入中提取并发送 `BDUSS`，其余 Cookie 项会被忽略，不会校验、保存或转发。

准备一个本人有权访问的非空测试分享。把 Cookie 复制到 macOS 剪贴板，然后执行：

```bash
uv run --frozen python -m app.providers.baidu.share_cli --cookie-clipboard
```

根据隐藏提示输入分享 URL 和可选提取码。探针只验证 Cookie 会话并读取分享根目录
第一页，不创建目录、不转存文件，也不保存 Cookie。成功报告应满足：

- `mode` 为 `readonly_web_share_api`；
- `checks.account.session_accepted` 为 `true`；
- `checks.share` 只包含数量和字段名；
- 报告中不应出现用户名、文件名、路径、Cookie、分享内部令牌或提取码。

## 第三阶段：单项转存写入探针

先在百度网盘中手工创建一个空目录，例如 `/MediaSync-Write-Probe`。准备一个本人有权
访问、且根层恰好只有一个测试项的分享；建议使用另一个账号创建，目标目录中不得已
存在同名项。

Cookie 或 BDUSS Value 放在剪贴板后执行：

```bash
uv run --frozen python -m app.providers.baidu.write_cli \
  --cookie-clipboard \
  --target-path /MediaSync-Write-Probe
```

按提示输入分享 URL、可选提取码，并准确输入确认短语
`WRITE ONE BAIDU TEST ITEM`。探针只提交一次写请求；网络超时或响应不确定时会保存本机
意图状态并禁止自动重放。成功后会在目标目录按文件名和大小二次核验，但不会自动删除
转存结果。

## 第四阶段：官方 OpenAPI 建目录探针

从 OpenList 获取页复制当前 Access Token 到剪贴板。在已存在的测试目录下选择一个不
存在的子目录路径，例如 `/MediaSync-Write-Probe/AutoFolderProbe`，然后执行：

```bash
uv run --frozen python -m app.providers.baidu.folder_cli \
  --token-clipboard \
  --target-path /MediaSync-Write-Probe/AutoFolderProbe
```

准确输入确认短语 `CREATE ONE BAIDU TEST FOLDER`。探针通过官方 OpenAPI 只提交一次
目录创建请求，随后重新列出父目录确认新目录存在；不会自动删除测试目录。
