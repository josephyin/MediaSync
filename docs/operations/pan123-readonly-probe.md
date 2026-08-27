# 123 云盘只读探针

这个探针用于确认 123 云盘 Web Token、账号根目录和测试分享的实际接口契约。
它只执行 GET 请求，不修改网盘内容，也不会把 Token 写入数据库、文件或日志。

## 准备

1. 在浏览器中登录 123 云盘。
2. 打开浏览器开发者工具的 Application（应用）→ Local Storage（本地存储）。
3. 在 123 云盘站点下找到 `authorToken`。只复制值，不要带键名。
4. 准备一个有效的 123 分享链接；支持传统 `/s/{分享码}` 和官方应用返回的
   `{UID}.share.123pan.cn/123pan/{分享码}` 格式。如有提取码，稍后在隐藏输入框填写。

不要把 Token、提取码或完整浏览器存储截图发到聊天、Issue 或日志中。

## 推荐执行方式（macOS 剪贴板）

先把 `authorToken` 的值复制到剪贴板，然后在仓库根目录执行：

```bash
cd backend
uv run --frozen python -m app.providers.pan123.cli --token-clipboard --check-share
```

命令会继续询问分享链接和可选提取码。最终只输出条目数量和字段名，不输出账号名、
文件名、文件 ID、Token 或提取码。

## 隐藏输入方式

```bash
cd backend
uv run --frozen python -m app.providers.pan123.cli --check-share
```

终端提示 `123 Access Token (hidden, not saved):` 时粘贴 Token 并回车。隐藏输入没有
回显属于正常现象。

## 验收条件

- `provider` 为 `pan123`；
- `mode` 为 `readonly_private_api`；
- `persisted` 为 `false`；
- `checks.account.session_accepted` 为 `true`；
- `checks.root` 和 `checks.share` 均包含 `item_count`、`total_count`、`field_names`；
- 输出中不出现 Token、账号名、文件名、文件 ID 或提取码。

只读探针通过后，才进入 `LoginUuid` + Token 的单项转存写入验收。写入验收必须使用
另一个账号创建的分享，并保存到专用空目录。

## 单项写入验收

测试分享必须只包含一个顶层项目，目标目录中不能存在同名项目。将 `authorToken` 复制到
macOS 剪贴板后执行：

```bash
cd backend
uv run --frozen python -m app.providers.pan123.write_cli \
  --token-clipboard \
  --target-folder-id 0
```

`LoginUuid` 可以从浏览器 Local Storage 复制并隐藏粘贴；也可以直接回车，由探针生成一个
仅在当前进程使用的 UUID。确认写入时必须准确输入：

```text
WRITE ONE TEST ITEM
```

探针会在 POST 前把不含凭证的写入意图保存到系统临时目录。POST 超时、连接中断、服务端
错误或非 JSON 响应都视为结果不确定，不自动重放。成功提交后会轮询目标目录并按同名项目
确认写入；重跑相同命令只会恢复验证，不会再次提交。

## 建目录验收

正式订阅会按相对路径逐级创建目录，因此还需单独验证建目录契约：

```bash
cd backend
uv run --frozen python -m app.providers.pan123.folder_cli \
  --token-clipboard \
  --parent-folder-id 0 \
  --folder-name MediaSync测试
```

确认时准确输入 `CREATE ONE TEST FOLDER`。如果目录已经存在，探针只验证并返回，不会重复
创建。请求结果不确定时会保留写入意图并禁止再次提交。
