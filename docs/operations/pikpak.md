# PikPak 接入（实验性）

当前为源码实现。2026-09-18 已通过本机真实账号登录、Token 刷新和网盘容量校验；
分享转存及默认接收目录到目标目录的移动已通过本机单文件实测；整批、人机验证码和 NAS 部署仍待验收。
现有 v0.2.1 镜像不包含此功能。通过离线测试不代表上游接口当前可用。

## 账号密码登录（推荐）

MediaSync → 云盘账号 → 添加 → PikPak → 登录账号，填写显示名称、PikPak 邮箱或手机号和密码。
本机后端直接调用 PikPak 官方 captcha/init 和 signin 接口，不保存密码，登录成功后
仅加密保存 Refresh Token、客户端标识及设备标识。随后点击验证检查网盘访问能力。
若触发人机验证，本次请求停止，不自动重试或打开任意上游链接；请在官方网页
处理后重试。第三方联合登录、验证码自动完成和扫码登录暂不支持。

## 已有 Token 添加账号（备用）

1. 在 PikPak 官方网页登录。在浏览器开发者工具 Network 中找到
   `user.mypikpak.com/v1/auth/token`（刷新）或 `/v1/auth/signin`（登录）的响应，
   在本机复制 `refresh_token`，不要分享响应或截图。
2. MediaSync → 云盘账号 → 添加 → PikPak → 填写凭证。
   普通网页 Token 可直接粘贴；来自其他客户端时必须提供匹配的 `client_id`。
   也支持 JSON：`{"refresh_token":"…","client_id":"…","device_id":"…"}`。
   默认 client_id 为 PikPak Web 客户端；未填 device_id 时自动生成并随凭证加密保存。
3. 点击验证，确认账号，再选择目标目录并添加 `https://mypikpak.com/s/分享ID`
   或 `https://mypikpak.com/s/分享ID/子目录ID`。提取码使用订阅的独立密码字段。

不需要额外绑定 OpenAPI。支持账号密码登录，暂不提供扫码或自动处理人机验证码；
凭证被拒绝时在官方网页完成验证，再更新凭证。MediaSync 仅向固定 PikPak API
域名发送请求，不会请求用户填写的分享 URL，也不跟随 API 重定向。

## 转存与恢复

- 分享根目录直接使用 `/drive/v1/share` 返回的文件及分页；真实子目录使用
  `/drive/v1/share/detail`。链接带子目录 ID 时严格限制在该目录，404 不自动退回整个分享。
- 转存单个文件使用 `/drive/v1/share/restore`，实际先进入默认 `Pack From Shared` 目录；确认唯一指纹后使用 `files:batchMove` 移至目标目录并重新核验。
- 源文件必须具有 hash 和 size。订阅扫描递归展开目录并分别转存文件；
  不支持无法通过文件指纹核验的整目录转存。
- 转存确认响应后持久化本地核对标识（目标目录 + 文件名/大小/hash/类型的 SHA-256），
  此标识不是 PikPak 远端任务 ID。重启/重试沿用核对标识，不重发转存请求；必要时完成接收目录到目标目录的移动。
- 只有唯一匹配且 `PHASE_TYPE_COMPLETE` 的文件才报告完成；多个匹配进入待对账。
  这属于目标状态核验，并非远端任务来源证明；请避免其他工具同时写入测试目标。
- 超时、5xx、无法识别的写入响应进入 `PROVIDER_WRITE_UNCERTAIN`，停止自动重发。
  API 不自动重试写请求。人工核对 PikPak 后再决定如何恢复。
- 空页终止；相同游标、循环游标或无新文件的内部目录遍历会报分页异常。
- 日志/异常不包含上游响应正文、Token 或分享授权 Token。Refresh Token 轮换
  沿用 MediaSync 的加密持久化流程。

## 真实验收（部分完成）

登录后的网盘请求已补充 Web captcha/init 验证令牌与 X-Captcha-Token；
验证令牌仅在内存缓存，过期重新申请，遇到交互验证停止请求。

后续使用自有账号、专用空目录以及另一个账号的单文件测试分享，依次验证：
账号验证与重启后 Token 刷新；多页及子目录浏览；提取码及失效分享；创建目标目录；
提交一次转存、pending 后恢复核对、最终目标 ID/大小/hash；重复扫描不重复写入。
还需检查会员/配额、网络可达性和风控限制。不得将离线 MockTransport 响应当作真实证据。

## 协议参考

- [PikPakAPI](https://github.com/Quan666/PikPakAPI/blob/main/pikpakapi/__init__.py)：刷新凭证、文件和分享接口。
- [pikpaktui 分享实现](https://github.com/Bengerthelorf/pikpaktui/blob/main/src/pikpak/share.rs)：分享分页参考；转存目标参数以实测为准。
- [OpenList PikPak](https://github.com/OpenListTeam/OpenList/blob/main/drivers/pikpak/util.go)：Web 客户端标识。

本适配器独立实现，仅参考以上协议字段；不是 PikPak 官方 SDK。
