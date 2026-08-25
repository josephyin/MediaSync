# 夸克网盘只读诊断手册

- 状态：Q2 实时验证入口
- 日期：2026-08-25
- 写操作：无
- 凭证持久化：无

该诊断只验证账号会话、根目录第一页和可选测试分享的第一页。它不会创建账号、修改
数据库、创建目录或转存文件，也不会把夸克加入 MediaSync Provider 注册表。

## 1. 安全边界

- 只在运行命令的本机终端输入 Cookie，不要粘贴到聊天、Issue、日志或截图。
- Cookie 和可选分享密码使用隐藏输入，不进入命令行参数或 Shell history。
- 诊断结果不包含账号昵称、用户 ID、文件名、文件 ID、`stoken`、Cookie 或分享
  密码，只输出数量、字段名称和错误分类。
- 响应中的 `__puus`、`__pus` 只在进程内临时合并；诊断结束后丢弃。
- 所有请求只发往代码内固定的 `pan.quark.cn` 和 `drive.quark.cn`，不接受自定义
  API host，也不跟随重定向。

Cookie 等同账号登录权限。若怀疑已经泄露，应立即在夸克侧退出相关登录会话，不要
继续使用该 Cookie。

## 2. 准备 Cookie

1. 在自己的浏览器登录 `https://pan.quark.cn/`。
2. 打开浏览器开发者工具的 Network 面板并刷新夸克网盘页面。
3. 选择发往 `pan.quark.cn` 或 `drive.quark.cn` 的账号/目录请求。
4. 从 Request Headers 复制完整 `Cookie` 值，只保留在本机剪贴板直到输入诊断。
5. 不要复制浏览器 Profile、Cookie 数据库、HAR 或整组请求头。

诊断工具会拒绝换行、控制字符、重复 Cookie 名、异常超长内容和不符合 Cookie
键值格式的输入。

## 3. 运行账号与根目录检查

先按 README 完成本地后端开发环境，再从 `backend/` 目录执行：

```bash
.venv/bin/python -m app.providers.quark.cli
```

终端出现以下提示后粘贴 Cookie 并回车，输入不会回显：

```text
Quark Cookie (hidden, not saved):
```

随后会实时显示当前只读阶段，例如：

```text
[1/2] 正在验证账号 Cookie…
[2/2] 正在读取根目录第一页…
```

默认每个请求最多等待 15 秒；基础检查有两个顺序请求，因此网络异常时最多可能等待
约 30 秒。需要快速定位时可以临时使用：

```bash
.venv/bin/python -m app.providers.quark.cli --timeout 8
```

如果长时间没有新输出，可以按 `Ctrl+C` 安全取消；工具会退出且不保存 Cookie。

如果当前终端的隐藏输入无法确认回车是否生效，可以在 macOS 上先把 Cookie 放入
剪贴板，然后使用标准输入模式：

```bash
pbpaste | .venv/bin/python -m app.providers.quark.cli --cookie-stdin --timeout 8
```

Cookie 内容不会出现在命令参数或 Shell history 中。命令读取剪贴板后会立即显示：

```text
Cookie 已接收（仅在当前进程内使用，不保存）。
```

成功输出示例：

```json
{
  "checks": {
    "account": {
      "session_accepted": true
    },
    "root": {
      "field_names": [
        "fid",
        "file_name"
      ],
      "item_count": 10,
      "total_count": 120
    },
    "rotated_cookie_names": [
      "__puus"
    ],
    "share": null
  },
  "mode": "readonly_private_api",
  "persisted": false,
  "provider": "quark",
  "schema_version": 1
}
```

数量只是接口结构验证结果，不会写入 MediaSync。

## 4. 增加测试分享检查

使用维护者自有、可随时失效且不包含敏感文件的测试分享：

```bash
.venv/bin/python -m app.providers.quark.cli --check-share
```

如果隐藏 Cookie 输入在当前终端不工作，可以继续使用剪贴板模式；分享 URL 和密码仍
由当前终端交互读取：

```bash
.venv/bin/python -m app.providers.quark.cli --cookie-clipboard --check-share --timeout 8
```

该命令直接调用 macOS 的 `/usr/bin/pbpaste`，Cookie 不进入命令参数或 Shell
history；标准输入保持连接当前终端，用于读取分享 URL 和可选密码。

工具随后分别提示输入分享 URL 和可选密码。分享 URL 必须使用
`https://pan.quark.cn/s/<share-id>`，不要把 `?pwd=` 密码放入 URL；密码通过隐藏
提示单独输入。

该操作只获取 `stoken` 并读取分享根目录第一页，不执行保存。

## 5. 结果分类

| 错误码 | 含义 | 下一动作 |
|---|---|---|
| `QUARK_INPUT_INVALID` | Cookie、URL 或参数格式不安全 | 修正本地输入，不要放宽校验 |
| `QUARK_AUTH_EXPIRED` | Cookie 过期或未授权 | 在夸克网页重新登录并重新复制 Cookie |
| `QUARK_SHARE_INVALID` | 分享失效、密码错误或无权访问 | 换维护者自有测试分享或检查密码 |
| `QUARK_RATE_LIMITED` | 上游明确限流 | 停止重复运行，稍后再试 |
| `QUARK_RISK_CONTROL` | 验证码、风控或额外校验 | 停止自动诊断，在夸克官方页面处理 |
| `QUARK_UPSTREAM_CHANGED` | 返回结构不符合已验证契约 | 不继续实施，先更新解析和测试 |
| `QUARK_PROBE_FAILED` | 网络、超时或其他上游拒绝 | 保存错误码和时间，不保存 Cookie 或响应正文 |

命令成功退出码为 `0`，失败为 `1`。报告可以用于 Q2 验收，但不得连同终端历史、
Cookie、HAR 或真实响应一起提交。

## 6. Q2 验收记录

实时验证后只记录：

- 日期、操作系统和 MediaSync 提交 SHA；
- 账号、根目录和测试分享三项是否通过；
- 脱敏错误码；
- 是否观察到允许的 Cookie 键轮换；
- 分页响应是否包含预期总数和字段名称。

不要记录账号昵称、用户 ID、真实文件名、`fid`、`stoken` 或任何凭证值。Q2 通过
仍只允许继续开发只读 Provider，不自动批准凭证持久化或分享转存。

### 6.1 2026-08-25 实时验收

- 环境：macOS 26.1（Build 25B78）。
- MediaSync 基线提交：`3fcaa124b17a7a88992540d8ca44a6511eb2920c`；诊断代码为
  当时工作区未提交改动。
- 账号会话：通过。
- 根目录第一页：通过；响应包含总数和预期结构字段。
- 测试分享第一页：通过；响应包含总数和预期结构字段。
- 脱敏错误码：无。
- 允许的进程内 Cookie 轮换：观察到 `__puus`。
- 持久化和写操作：均未发生。

该记录证明 Q2 真实只读接口门禁通过；正式只读 Provider 随后已以部分能力注册，并只
允许白名单 Cookie 轮换进入现有加密存储。该结论不批准目录创建或分享转存。
