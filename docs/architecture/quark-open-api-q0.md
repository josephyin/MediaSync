# 夸克 OpenAPI 托管授权 Q0 门禁

- 日期：2026-08-25
- 状态：适配已完成离线实现；公共 OpenList token 不足以完成真实 OpenAPI 验收
- 决定：Cookie 私有接口负责完整分享链路；OpenAPI 仅作为可选账号盘能力
- 运行时影响：Cookie 路线已以实验性状态启用订阅；OpenAPI 不作为前置条件

## 1. 为什么重新评估路线

夸克官方发布的 Quark Drive Skill 表明存在浏览器 OAuth，并声称通过夸克开放平台 API
完成账号文件操作。OpenList 也提供 `QuarkOpen` 驱动和类似阿里云盘的在线 token
broker。这证明“托管授权 + OpenAPI”值得验证，但不能据此假设 OpenList token 可以
替代业务 API 所需的 AppID、SignKey 和公共请求头，也不能替代分享链路。

但“能够登录和浏览自己的网盘”不等于“能够完成 MediaSync 的分享订阅和转存”。
MediaSync 的最小闭环至少需要：

1. 可轮换且可安全保存的账号授权；
2. 分享 URL 解析和分页浏览；
3. 分享项转存到指定目录；
4. 异步写入结果可恢复、可对账、不会重复提交。

## 2. 当前证据

### 2.1 OpenList QuarkOpen

核查 OpenList 提交
[`1a6cabf`](https://github.com/OpenListTeam/OpenList/tree/1a6cabf45aecf66c6d2ff6c32aed39d50264f43c/drivers/quark_open)
中的 `quark_open` 驱动后，可确认它使用 `https://open-api-drive.quark.cn`，并实现
自有网盘账号信息、目录列表、下载、建目录、上传、移动、改名和删除。

它没有实现分享 URL 解析、分享目录浏览或分享转存；`Copy` 也明确返回不支持。
因此不能把该驱动直接映射为 MediaSync 的 `share_save` Provider。

### 2.2 OpenList-APIPages token broker

[OpenList-APIPages](https://github.com/OpenListTeam/OpenList-APIPages/tree/a8b109d8399c8e2dd53a91145ff78967087808e0)
提供 `/quarkyun/requests`、`/callback` 和 `/renewapi`，与阿里云盘的托管授权模式相似。
截至 2026-08-25，公共站点仍在线，缺少参数的刷新请求会返回结构化错误；本次没有
向公共站点发送任何凭证。

当前 broker 具有以下已知信任边界：

- `/quarkyun/renewapi` 只注册 GET；
- refresh token 从 `refresh_ui` 查询参数读取；
- 查询参数可能进入反向代理、WAF、CDN、浏览器历史和访问日志；
- broker 随后还依赖 `oauth.fnnas.com` 完成实际授权和刷新，形成额外信任边界；
- 自建 broker 虽可控，但仍需要合法来源的 AppID/SignKey，不能复制公共共享凭证。

维护者已明确选择与现有阿里云盘 OpenList 模式相同的风险边界。MediaSync 因此兼容
这个 GET 刷新协议，但不会在异常或日志中输出完整请求 URL；界面明确提示 token 会
发送给所选 broker。自建 POST broker 仍是后续推荐改进，不是本轮阻断项。

### 2.3 官方 Quark Drive Skill

[夸克官方 Quark Drive Skill](https://b.quark.cn/apps/quarkcloud_skill_info/routes/V-sc7gOBT)
公开说明包含 OAuth、分享详情和 `saveas` 转存能力，说明完整产品能力是存在的。
但公开包对核心 CLI 实现设有不可读取边界，公开说明也没有给出可供 MediaSync 服务端
独立实现和维护的端点、签名、token 刷新及服务条款。分享详情文档还明确称其通过
“网盘服协议”请求，不能据此推断分享链路属于稳定 OpenAPI。

MediaSync 不把该不透明 CLI 嵌入后端，也不通过执行外部工具绕过 Provider 契约、
凭证存储和幂等对账要求。

对官方 v1.0.9 包的公开文档和安装脚本继续核查后，还确认：

- 交付物是面向 Agent 的 Node.js CLI，不是有稳定接口和依赖声明的服务端 SDK；
- CLI 自行在线检查版本并覆盖更新，不能满足 MediaSync 精确版本锁定和供应链复核；
- 授权状态由 CLI 写入本地 `config.json`，不经过 MediaSync 的加密凭证模型；
- 文档要求命令携带用户原始提问和会话标识用于服务质量追踪，这与 MediaSync 默认
  不把订阅名称、路径和操作意图发送给额外分析服务的边界不一致；
- 发布包中未提供可供 MediaSync 复用、再分发和审计的 SDK 许可证说明。

本次只下载并静态检查官方页面当时提供的 zip，没有安装、执行或登录。检查样本的
SHA-256 为 `2426966eb01efe539024652aa91e61a7f700621c7e77fb4b431d5e6ec8640583`；
该值只标识 2026-08-25 的研究样本，不表示对未来下载包的信任或批准。

因此即使官方 CLI 的个人授权和 `saveas` 能运行，也只能证明产品能力，不能批准把
CLI 安装进 MediaSync 镜像、挂载它的配置目录或由后端以子进程调用。

## 3. Q0 决定

维护者已取得 OpenList Refresh Token，但真实探针确认业务请求仍缺少
`x-pan-client-id`、`x-pan-tm` 和 `x-pan-token` 所依赖的配套应用参数。因此当前决定为：

- Cookie 私有 Provider 负责账号会话、分享解析和分享目录读取；
- OpenList Open Provider 负责 access token 刷新、账号盘、目录分页和目录创建；
- Open Refresh Token 使用现有加密字段保存；SignKey 加密保存，AppID 使用现有
  Client ID 字段；三者必须来自同一次匹配的授权配置；
- OpenAPI 不伪装成分享 Provider，分享方法明确返回能力不可用；
- 私有 `share_save` 已完成跨账号单项真实验收，`quark` 以实验性状态开放订阅；
- 两套凭证分别校验；私有会员接口没有稳定 user_id 时，界面要求维护者人工确认同账号。

这是职责分离的两套凭证，不是互为 fallback 的兼容分支。扫描和转存不能在 OpenAPI
与私有接口之间静默切换。

## 4. 进入实现的硬门禁

离线实现已完成。下面的门禁只适用于以后启用 OpenAPI 账号盘能力，不再阻塞 Cookie
分享订阅：

1. **应用凭证**：取得来源明确且与 token 配套的 AppID/SignKey；不能把授权页的
   “使用 OpenList 提供的参数”误认为业务 API 支持空参数，也不使用网上公开共享密钥。
2. **刷新边界**：明确选择可信 broker；MediaSync 不记录含 token 的 URL，并优先迁移
   到自建 POST broker。
3. **账号能力**：真实验证用户 ID、根目录列表、分页和 token 轮换。
4. **条款与限流**：确认允许服务器端自动化使用，并记录限流、过期和撤权行为。
5. **隐私与供应链**：服务端调用不强制上传用户原始请求文本；客户端或 SDK 可锁定
   精确版本，有明确许可证、校验值、变更记录和安全更新策略。

Mock 测试、OpenList 能运行、官方 Skill 能运行，都不能代替上述真实账号验收。

## 5. 后续执行顺序

1. 使用 Cookie 账号完成分享订阅；该路线已经通过真实转存验收。
2. 如需额外启用 OpenAPI 账号盘能力，绑定同一账号且互相匹配的 OpenList Refresh
   Token、AppID 和 SignKey，然后执行
   真实 OpenAPI 校验；缺少任一项时暂停 OpenAPI 路线。
3. 验证 Open 根目录与至少一个子目录分页；只记录脱敏结果。

部署前可先按[夸克 OpenList OpenAPI 只读验收](../operations/quark-openlist-probe.md)
运行无持久化诊断。

## 6. 向夸克确认的最小问题清单

申请正式接入时只需要确认下面这些问题，不发送真实用户数据或现有 Cookie：

1. 是否开放面向第三方服务器应用的夸克网盘 OpenAPI，申请入口和准入条件是什么；
2. 是否可以创建项目自有 AppID/SignKey，是否允许开源自部署软件由最终用户授权；
3. OAuth 回调、授权码交换和 refresh token 刷新是否有公开文档，能否由 MediaSync
   后端直接使用 POST 完成而不经过公共 broker；
4. 开放 API 是否包含分享详情、分享目录分页和分享文件转存，而不只是自有网盘文件
   管理；
5. 转存是否返回可恢复查询的 task/operation ID 和最终目标 FID；
6. token、用户 ID、文件路径、分享 URL 和操作意图会被哪些服务处理、保存多久，是否
   可以关闭额外的会话文本或遥测上报；
7. 服务端自动化、并发、频率、配额、会员限制和商业/非商业使用边界是什么；
8. 是否提供可锁版本、有许可证和完整变更记录的 SDK，或者允许按照公开 HTTP 文档
   独立实现客户端。

只有获得可以留档的正式答复或文档后，才要求维护者进行浏览器 OAuth 验收；在此之前
不让维护者反复试 token，也不安装官方 CLI 到生产设备。
