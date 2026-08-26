# 变更日志

本项目的重要变更记录在此文件中。版本号遵循语义化版本。

## [0.2.0-rc.21] - 2026-08-26

这是 Docker Engine updater 身份字段规范化兼容修复候选版本。rc.20 现场更新已
确认 updater 创建成功，但 Engine Inspect 把创建请求中的 `DAC_OVERRIDE` 返回为
`CAP_DAC_OVERRIDE`，并把 `no-new-privileges:true` 返回为
`no-new-privileges`。此前的原始字符串完全匹配因此误判合法 updater 身份，助手
容器被 `unless-stopped` 策略反复重启。

### 修复

- updater 身份校验在比较前规范化 Linux capability 的等价 `CAP_` 前缀。
- `no-new-privileges:true`、`no-new-privileges=true` 与 Docker Inspect 返回的
  `no-new-privileges` 作为同一安全选项处理。
- 协调执行和恢复决策共用同一隔离边界校验，避免两条路径产生不一致判断。

### 安全边界

- 仍只接受唯一 `DAC_OVERRIDE`，要求 `CapDrop=ALL`，拒绝 `SYS_ADMIN` 等任何
  额外 capability。
- 仍只接受启用状态的 `no-new-privileges`；禁用、缺失或追加其他安全选项均拒绝。
- `NetworkMode=none`、只读根文件系统、挂载和设备映射边界均未放宽。

### 验证

- updater 定向测试 148 项、完整后端测试 663 项与 Ruff 检查通过。
- 使用 rc.20 镜像创建真实 Docker 容器，Inspect 返回规范化字段后由修复代码验证为
  `matched=true`；测试容器随后删除。
- 修复 PR #148 的后端与迁移、前端生产构建、单镜像与部署契约全部通过。

## [0.2.0-rc.20] - 2026-08-26

这是 NAS 受限数据目录快照兼容修复候选版本。现场确认部分 NAS 将宿主数据目录
映射为容器内 mode `000`、非 root 所有者，SQLite 文件同样为 `000`。Appliance
依靠 root 的 `DAC_OVERRIDE` 正常运行，而此前 updater 删除全部 capabilities 后
无法穿过 `/data`，因此在创建数据快照阶段安全回滚。

### 修复

- updater 继续删除全部 Linux capabilities，但精确恢复快照和回滚所必需的
  `DAC_OVERRIDE`。
- 恢复协调器把 `CapDrop=ALL`、唯一 `CapAdd=DAC_OVERRIDE` 和
  `no-new-privileges` 纳入严格身份校验；缺少能力或额外加入 `SYS_ADMIN` 等能力均
  拒绝自动恢复。
- 不修改 NAS 宿主目录权限，不要求用户手工 `chmod` SQLite 文件。

### 安全边界

- updater 仍保持 `NetworkMode=none`、只读根文件系统和 `no-new-privileges`。
- updater 只挂载 `/data` 与 Docker Socket，不继承 MediaSync 的设备映射。
- 候选 MediaSync 容器仍只继承经过严格校验的普通 `HostConfig.Devices`；复杂
  `DeviceRequests` 继续拒绝。

### 验证

- 后端 654 项测试和 Ruff 检查通过。
- Docker 恢复演练把测试卷根目录设置为 mode `000`，并在仅保留
  `DAC_OVERRIDE` 的条件下验证持久锁、helper 崩溃重启、恢复写入和安全退出。
- 修复 PR #146 的后端与迁移、前端生产构建、单镜像与部署契约全部通过；
  rc.20 标签仍需以发布提交的 CI 为准。

## [0.2.0-rc.19] - 2026-08-25

这是 Web 一键更新自动回滚原因可见性修复候选版本。rc.17 向 rc.18 的现场升级
已确认能够安全回滚，但回滚终态没有保留触发失败的阶段，系统设置页面无法提供
可诊断信息。

### 修复

- updater v1 在进入回滚和发布 `rolled_back` 终态时保留脱敏失败阶段与稳定错误码。
- updater v2 根据最后完成的安全检查点生成公开失败阶段，并在可恢复回滚的所有
  检查点之间持续保留，最终由 Appliance 对账写入更新操作记录。
- 历史回滚记录没有错误说明时，系统设置明确提示该次更新未记录具体失败阶段，
  不再仅显示目标版本造成误解。

### 安全边界

- 页面和持久化记录只包含预定义阶段名称，不写入异常原文、容器标识、路径、
  Docker 响应、Cookie 或 Token。
- 自动回滚、设备映射校验、快照恢复和 updater 助手权限边界没有放宽。
- rc.18 的既有回滚记录无法事后恢复已经丢失的原始异常；rc.19 只保证后续尝试
  能留下可操作的脱敏阶段信息。

### 验证

- 后端 650 项测试和 Ruff 检查通过。
- 前端 5 项测试与生产构建通过。
- 修复 PR #144 的后端与迁移、前端生产构建、单镜像与部署契约全部通过；
  rc.19 标签仍需以发布提交的 CI 为准。

## [0.2.0-rc.18] - 2026-08-25

这是夸克网盘实验性 Provider 候选版本，同时补齐通用 Provider 异步写入的持久化
恢复边界。维护者已确认 rc.17 稳定性观察通过；rc.18 仍需完成公开镜像与升级冒烟。

### 新增

- 新增夸克 Cookie 私有接口账号验证、分享浏览、目标目录浏览与创建，以及分享转存。
- 新增单项只读和写入诊断命令；凭证只在当前进程使用，输出统一脱敏。
- 新增可选的 OpenList OpenAPI 账号盘适配。该能力需要同一授权配置中的 Refresh
  Token、AppID 和 SignKey，不是分享订阅的必填项。
- 前端按 Provider 能力开放实验性夸克账号与订阅入口。

### 可靠性

- `tasks` 新增通用 write intent、远端 operation ID、状态和脱敏结果字段；Worker
  重启后只恢复查询已有远端任务，不重复提交写请求。
- 写入超时、连接中断、上游 `5xx` 或已接受请求但缺少 operation ID 时进入
  `uncertain`，停止自动重试并要求人工对账。
- 夸克 Cookie 轮换继续使用现有加密凭证持久化路径，不写入任务 payload 或日志。

### 验证与限制

- 使用另一个夸克账号创建的单项分享完成真实转存，提交、远端完成、目标 ID 和目标
  目录复核均通过。
- 目标账号不能把自己创建的分享转存回同一账号；实测会返回
  `HTTP 404 / code 41017`，该错误作为确定拒绝停止重试。
- 后端 650 项测试、前端测试与生产构建、全新数据库迁移和 CI 单镜像 Appliance
  冒烟均通过。

### 升级说明

- 从 rc.17 升级会自动执行 `0008_provider_operations` 数据库迁移，只增加可空任务
  字段，不修改已有订阅、文件或历史任务结果；升级前仍应备份完整 `/data`。
- 现有阿里云盘配置不需要修改。使用夸克时需要在账号页面输入目标账号 Cookie；
  分享必须由另一个账号创建。
- 本版本仍是候选版本；夸克与阿里云盘 Web 私有接口均可能受上游变化或风控影响。

## [0.2.0-rc.17] - 2026-08-17

这是 NAS 一键更新设备映射兼容补丁。此前只要当前容器存在 Docker `Devices`
映射，更新器就会在准备阶段一律拒绝，无法继承 NAS 为容器配置的设备。

### 修复

- 严格校验并保存当前容器的设备宿主路径、容器内路径和 `rwm` 权限组合，在候选
  MediaSync 容器中原样恢复。
- updater 助手容器本身不继承任何设备权限；设备只交给替换后的 MediaSync
  容器，避免扩大更新控制面的权限。
- 非法路径、重复容器内目标、非法权限和复杂 GPU `DeviceRequests` 继续拒绝，
  防止静默丢失或改变设备配置。

### 验证

- 增加有效 `/dev/dri` 映射继承测试，覆盖容器检查、私有 handoff 和候选创建。
- 增加无效字段、相对路径、非法权限及 handoff 篡改的拒绝测试。

### 升级说明

- rc.16 用户仍需在 NAS 容器管理器中手工重建到 rc.17 一次，并保持原端口、
  环境变量、Docker Socket、设备映射和 `/data`；进入 rc.17 后，普通 `Devices`
  映射可随一键更新继承。
- 本版本没有数据库迁移、Task Engine 或 Provider 行为变化。

## [0.2.0-rc.16] - 2026-08-17

这是系统设置更新结果展示修复候选版本。此前升级到新版本后，数据库中保留的
旧版本失败记录仍会显示为当前失败，造成新版本再次更新失败的误解。

### 修复

- 正在执行的更新操作始终展示；已经结束的结果只在与当前运行版本相关时展示。
- 失败、取消和已回滚结果按发起版本匹配；成功结果按目标版本匹配；自动回滚
  失败同时匹配发起版本和目标版本，以保留需要人工处理的告警。
- 更新操作审计记录继续保存在数据库中，不执行修改或删除。

### 验证

- 增加版本结果匹配的参数化测试，覆盖失败、取消、成功、回滚及非法版本格式。
- 增加更新状态接口回归测试，确认 rc.11 等旧版本留下的失败结果在新版本运行时
  不再返回给系统设置页面。

### 升级说明

- rc.15 用户可以直接在系统设置中一键更新到 rc.16；更新完成后旧失败提示会自动
  消失，不需要清理 `/data` 或修改数据库。
- 本版本没有数据库迁移、Task Engine 或 Provider 行为变化。

## [0.2.0-rc.15] - 2026-08-17

这是 rc.14 容器识别修复的边界补丁。实际公开镜像验证发现，NAS 保留已停止的
旧 MediaSync 容器时，解析器会把历史容器也计入候选，导致无法唯一识别当前容器。

### 修复

- 自定义 hostname 的回退识别只在运行中的容器里选择当前官方 Appliance；已停止
  的旧容器不再参与候选计数。
- 同时存在多个运行中的官方 Appliance 时继续拒绝自动选择，保留安全边界。

### 验证

- 增加“一个运行容器加一个已停止历史容器”的回归测试。
- 使用自定义 hostname、Docker Socket 和已停止旧容器执行公开镜像验证，确认
  解析器返回当前运行容器的完整 64 位 ID。

### 升级说明

- rc.11 用户请直接手工重建到 rc.15，保留原端口、环境变量、Docker Socket 和
  `/data` 映射；无需先安装 rc.14。
- 本版本没有数据库迁移、Task Engine 或 Provider 行为变化。

## [0.2.0-rc.14] - 2026-08-17

这是 v0.2 稳定性观察期的 NAS 一键更新修复候选版本，解决飞牛、群晖等环境
使用自定义容器 hostname 时，能力探测显示可更新但安装阶段失败的问题。

### 修复

- 更新安装流程复用能力探测的安全容器解析器；当 hostname 不是 Docker 容器 ID
  时，通过官方 OCI 标签、`/data` 挂载和 Appliance 命令唯一解析当前容器，使用
  Docker 返回的完整 64 位 ID 执行更新。
- 保持原有安全边界：候选不唯一、非官方镜像、Compose 管理、缺少 `/data` 挂载
  或非 Appliance 模式时仍拒绝一键更新。
- 标签镜像发布成功后自动创建 GitHub Release，避免更新器只看到旧版本。

### 验证

- 增加自定义 NAS hostname 回归测试，覆盖解析完整容器 ID 后进入更新交接。
- 继续执行后端全量测试、前端测试与生产构建、双架构镜像构建和公开镜像验证。

### 升级说明

- 已受此问题影响的 rc.11 需要在 NAS 容器管理器中手工重建到 rc.14 一次；必须
  保留原端口、环境变量、Docker Socket 和 `/data` 映射。进入 rc.14 后，后续
  一键更新会使用修正后的容器识别流程。
- 本版本没有数据库迁移、Task Engine 或 Provider 行为变化。

## [0.2.0-rc.13] - 2026-08-17

这是 v0.2 稳定性观察期的发布恢复候选版本，包含 rc.12 的 Web 会话过期跳转
修复，并解决多架构镜像构建在 ARM64 前端依赖安装阶段挂起的问题。

### 修复

- 前端 builder 固定运行在 Buildx 的原生 `BUILDPLATFORM`；前端静态产物只构建
  一次并复用于 AMD64/ARM64 最终镜像，不再通过 QEMU 执行 `npm ci`。
- 发布 job 增加 30 分钟超时，构建异常时快速失败，避免占用 runner 达到 GitHub
  默认六小时上限。

### 验证

- 单镜像契约测试锁定原生前端 builder，防止后续重新引入 QEMU 前端构建。
- 继续执行前端测试、生产构建、后端全量测试和单镜像 CI 验证。

### 发布说明

- `v0.2.0-rc.12` 源码标签保持不可变；其首次镜像构建在 ARM64 `npm ci` 阶段
  挂起并被 GitHub 取消，没有作为可用容器版本发布。
- rc.13 不包含数据库模型、迁移、Task Engine 或 Provider 行为变化，可以直接
  复用 rc.11 及更早版本的 `/data`。

## [0.2.0-rc.12] - 2026-08-14

这是 v0.2 稳定性观察期的修复候选版本，修复管理员 Web 会话在页面停留期间
过期后，界面不会自动返回登录页的问题。

### 修复

- 统一 API 请求层在收到 `401 Unauthorized` 时立即清理前端登录状态，并自动
  跳转到登录页，无需手工刷新浏览器。
- 登录跳转会保留原站内页面地址，重新登录后返回会话过期前所在页面。
- 登录后的返回地址仅允许站内绝对路径，避免把外部地址作为跳转目标。

### 验证

- 增加 API 认证回归测试，覆盖 `401` 触发退出和其他错误不误触发退出。
- 前端生产构建继续通过 TypeScript 与 Vite 检查。

### 兼容性

- 本版本不包含后端 API、数据库模型、迁移、Task Engine 或 Provider 行为变化。
- 从 rc.11 升级时可以直接复用原有 `/data`、端口、环境变量和 Docker Socket
  配置；升级前仍应完整备份 `/data`。

## [0.2.0-rc.11] - 2026-08-04

这是 v0.2 可靠性基础的第十一个候选版本，在 rc.10 Updater 恢复地基上开放
实验性 Web 一键镜像更新，并让官方单容器新安装默认配置 Docker Socket。

### 新增

- 系统设置增加实验性一键更新按钮、管理员安装 API、执行状态和失败原因展示。
- 容器切换期间页面每 3 秒自动重连，恢复后继续显示最终更新结果。
- 新增 `compose.appliance.yml`，提供一个端口、一个数据目录和默认 Docker Socket
  的受维护单容器模板。
- Docker Run、Docker Hub、飞牛和群晖安装说明统一采用新的单容器默认契约。

### 可靠性

- 安装目标必须匹配最近一次版本检查结果，并锁定官方镜像的精确 digest。
- 更新前停止新任务领取并等待活动任务排空，再由持久 updater helper 接管切换。
- Updater v2 进度会同步到更新操作记录，候选容器失败时继续使用 rc.10 已验证的
  快照恢复和自动回滚路径。
- Launcher 会在终态对账前保留 helper 身份，并清理已经停止且解除重启策略的
  临时 helper。
- CI 新增单容器 Compose 端口、数据绑定、Socket、重启策略和 120 秒停止宽限
  契约验证。

### 安全与兼容性

- 官方单容器新安装默认映射
  `/var/run/docker.sock:/var/run/docker.sock`。Docker Socket 等同于宿主机 Docker
  管理员权限，只应交给可信官方镜像，管理端口不得直接暴露到公网。
- Docker 镜像无法自行绑定宿主机路径。群晖和飞牛从镜像创建容器时仍要求用户在
  图形界面手工确认 Socket 映射。
- 不需要一键更新时可以删除 Socket 映射；订阅、扫描、转存、日志和手动升级不受
  影响。
- 已有 rc.10 容器不会在升级镜像后自动获得 Socket。需要编辑或重建容器，并保留
  原 `/data`、端口、环境变量、重启策略和 120 秒停止宽限。
- 高级多容器 Compose 暂不支持内置一键更新，不得给所有服务挂载 Docker Socket。
- 本版本没有新增数据库迁移，仍使用 `0007_update_operations`；升级前必须停止
  容器并完整备份 `/data`。

## [0.2.0-rc.10] - 2026-08-03

这是 v0.2 可靠性基础的第十个候选版本，提供一键镜像更新的只读检查界面与
Updater v2 恢复地基。本版本用于 Docker、群晖和飞牛故障演练；安装 API 和 Web
更新按钮仍未开放。

### 新增

- 系统页面增加版本检查、发布通道和 Docker 能力只读状态。
- 新增更新操作持久化模型、排空闸门、目标镜像 digest 与 OCI 标签校验。
- 新增 `/data` 完整快照、Updater 交接文档、候选容器证据和健康验证契约。
- 新增 `python -m app.updater` 恢复协调器运行入口。

### 可靠性

- Updater 采用两阶段提交，`COMMIT_REQUESTED` 之后禁止恢复旧快照或启动旧容器。
- 前向与回滚路径使用持久检查点，可在副作用完成但检查点尚未写入时对账恢复。
- 持久 helper 使用独占 `flock`、严格容器身份校验和 restart-policy fencing。
- 新 helper 接管时使用 recovery generation 限制，最多自动恢复 3 代。
- 终态 helper 必须确认重启策略解除后退出，并由严格清理服务移除。

### 验证

- 前向和回滚故障矩阵覆盖副作用调用前、成功后以及检查点写入前后。
- CI 使用真实 Docker 验证 helper 异常重启、重新持锁、解除重启策略和释放锁。
- 提供群晖、飞牛真实 NAS 故障演练脚本、证据矩阵和失败关闭说明。

### 安全与兼容性

- Docker Socket 仍是可选高权限能力，普通部署不需要也不会默认挂载。
- 本版本不会从 Web 发起镜像安装；用户不得把只读版本检查误认为一键更新已开放。
- 数据库会自动升级到 `0007_update_operations`；升级前必须停止容器并完整备份
  `/data`，不得只备份 SQLite。
- 现有单容器部署继续使用一个 `9090` 端口和同一个 `/data` 映射。

## [0.2.0-rc.9] - 2026-07-31

这是 v0.2 可靠性基础的第九个候选版本，修复 Task Engine v2 执行数据在任务
中心缺失的问题。

### 修复

- 任务列表和详情从最新 TaskRun 投影执行摘要、开始时间和结束时间。
- 任务中心使用 v2 的 `retry_count` 和 `max_retries` 显示真实重试进度。
- 终态任务不再把空的 `next_attempt_at` 显示为无含义空值，而是区分无需重试、
  已停止重试和已取消。
- 补齐等待重试、等待凭证、正在取消和已取消等 v2 状态的中文显示。
- 已有 TaskRun 历史升级后即可显示，不需要重新执行任务。

### 兼容性

- 本版本不包含数据库模型、迁移、Task Engine 状态机或 Provider 行为变化。
- 从 rc.8 升级可直接复用原有 `/data` 和 `9090:9090` 端口映射。
- 升级前仍应停止容器并完整备份 `/data`。

## [0.2.0-rc.8] - 2026-07-30

这是 v0.2 可靠性基础的第八个候选版本，重点补齐正式版前的工程质量门禁、
管理员凭证运维能力和备份恢复证据。

### 新增

- 默认单容器 Appliance 支持在管理后台在线修改管理员密码。
- 密码修改或离线重置后，全部旧登录会话立即失效。
- 管理员密码和会话修订号作为一个持久化单元原子更新。
- 新增 SQLite 数据库、运行时密钥和凭证密钥整体备份恢复演练记录。

### 工程化

- Pull Request 持续集成统一执行 Ruff、后端全量测试和前端生产构建。
- 版本发布继续从同一次构建同步 GHCR 与 Docker Hub，并验证双架构清单。

### 安全

- 在线修改必须校验当前密码，新密码至少 8 个字符且不能继续使用 `admin`。
- 密码不会写入 API 响应、浏览器持久化存储或应用日志。
- 高级 Compose 继续通过 `.env` 管理密码，界面会明确提示离线修改方式。

### 兼容性

- 本版本不包含数据库模型、迁移、Task Engine 或 Provider 行为变化。
- 从 rc.7 升级时可直接复用原有 `/data` 和 `9090:9090` 端口映射。
- 升级前仍应停止容器并完整备份 `/data`。

## [0.2.0-rc.7] - 2026-07-29

这是 v0.2 可靠性基础的第七个候选版本，统一调整普通用户和 NAS 图形化安装时
显示的默认 Web 端口。

### 改进

- Appliance 的 Nginx 监听端口从 `8080` 调整为 `9090`。
- 单镜像只声明一个 `9090/tcp`，飞牛和群晖创建容器时使用
  `9090:9090`。
- Docker Compose 默认宿主机端口调整为 `9090`。
- 健康检查、CORS 默认来源、Docker Hub Overview 和当前部署文档同步使用
  `9090`。

### 兼容性

- 从 rc.6 升级时需要把端口映射从 `8080:8080` 改为 `9090:9090`。
- 本版本不包含数据库、迁移、Task Engine、Provider 或持久化数据变化。
- 原有 `/data` 可以直接复用；升级前仍应完整备份。

## [0.2.0-rc.6] - 2026-07-28

这是 v0.2 可靠性基础的第六个候选版本，重点修复低性能 NAS 上 Docker 健康
检查容易超时的问题，并加入正式 MediaSync 品牌图标和群晖安装说明。

### 新增

- 新增播放与同步语义结合的 MediaSync SVG 品牌图标。
- 浏览器 favicon、登录页和管理后台侧边栏统一使用正式图标。
- 新增群晖 DSM Container Manager 中文安装教程。
- 明确群晖不会自动创建宿主机 `/data` 文件夹映射，创建容器时必须手工添加。

### 修复

- Docker 健康检查超时从 5 秒调整为 15 秒。
- 启动宽限从 30 秒调整为 60 秒，适应启动较慢的 NAS。
- 健康检查改为通过 Nginx 代理检查 API，一次请求同时覆盖 Web 入口与 API，
  避免重复串行探测导致假超时。
- 保留 Launcher、Nginx、API、Scheduler 和 Worker 五组件健康语义。

### 兼容性

- 本版本不包含数据库、迁移、Task Engine 或 Provider 行为变化。
- 端口、`/data`、默认管理员密码和已有密码升级保护契约不变。
- 从 rc.5 升级时复用原有 `/data`，无需额外迁移步骤。

## [0.2.0-rc.5] - 2026-07-28

这是 v0.2 可靠性基础的第五个候选版本，保留 rc.4 的飞牛友好镜像默认值，并
修复已有数据目录升级时管理员密码可能被默认值覆盖的问题。

### 修复

- 新增 `IMAGE_DEFAULT_ADMIN_ONLY=true` 镜像标记。
- 全新 `/data` 仍默认使用用户名 `admin`、密码 `admin`。
- 已有 `/data` 会忽略镜像自带的 `admin` 默认值，继续保留原管理员密码。
- 用户显式填写其他 `ADMIN_PASSWORD` 时仍可离线重置密码。
- 增加旧密码保留和自定义密码覆盖测试。

### 安全说明

- rc.4 不建议用于已有数据目录升级；请直接使用 rc.5。
- 默认密码仅用于全新安装，首次登录后必须改为强密码。
- 加密密钥生成、持久化和拒绝静默轮换规则不变。

## [0.2.0-rc.4] - 2026-07-28

这是 v0.2 可靠性基础的第四个候选版本，重点优化飞牛 fnOS 等图形化 Docker
平台的首次安装默认值。

### 改进

- Appliance Web 入口统一使用容器端口 `8080`。
- 镜像只声明一个 `8080/tcp`，不再让图形界面自动生成内部 API `8000` 映射。
- 镜像声明 `/data` 持久化卷，飞牛创建容器时可以自动显示容器路径。
- `ADMIN_PASSWORD` 默认值为 `admin`，图形界面无需手工补齐即可首次登录。
- 健康检查改为探测 Appliance 的 `8080` 入口。
- 更新 Docker、飞牛和 Docker Hub 中文安装说明。

### 安全说明

- 默认用户名和密码均为 `admin`，只用于受信任局域网的首次安装。
- 首次登录后必须通过 `ADMIN_PASSWORD` 设置自己的强密码并重建容器。
- 使用默认密码时不得把管理端口暴露到公网。
- 加密密钥仍由 Appliance 随机生成并持久化，不使用固定默认值。

### 兼容性

- 本版本不包含数据库模型、迁移、Task Engine 或 Provider 行为变化。
- 单容器部署从 `8080:80` 改为 `8080:8080`。
- 高级多容器 Compose 继续使用宿主机 `8080` 到前端容器 `80` 的映射，职责和
  内部端口契约不变。

## [0.2.0-rc.3] - 2026-07-28

这是 v0.2 可靠性基础的第三个候选版本，重点把单镜像进一步变为普通 NAS 用户
可直接运行的单容器 Appliance。

### 新增

- 默认镜像入口自动完成持久化配置、数据库迁移、旧任务对账和常驻进程启动。
- 内置 Launcher 独立监管 Nginx、API、Scheduler 和 Worker。
- 首次启动自动生成并持久化签名密钥、凭证加密密钥和管理员密码。
- Unix Socket 本地状态接口和五组件 Docker 聚合健康检查。
- Docker 单容器、飞牛 fnOS、备份、升级和回滚中文文档。
- Tag 发布后自动同步 Docker Hub 中文 Overview。

### 改进

- 普通用户只需映射一个 Web 端口和一个 `/data` 目录即可部署。
- API、Scheduler 和 Worker 在单容器内仍保持独立操作系统进程和职责边界。
- 迁移或对账失败时 Worker 不会启动，关键进程崩溃时容器整体快速失败。
- `SIGTERM` 按 Nginx、Scheduler、Worker、API 顺序优雅停止。
- 自动生成的管理员密码只在首次启动时输出一次。
- `SESSION_COOKIE_SECURE` 与运行环境名称解耦，局域网 HTTP 默认可以正常登录。
- Appliance 和高级多容器 Compose 继续复用同一镜像和业务实现。

### 兼容性

- 本版本不包含数据库模型或迁移变化。
- 从 rc.2 Compose 升级时，第一次启动必须提供原有签名密钥、凭证加密密钥和
  管理员密码；Appliance 会把它们持久化到 `/data/config/runtime-secrets.json`。
- 数据库与运行时密钥必须作为一个整体备份和恢复。
- 新旧部署不得同时访问同一个 SQLite 数据库。

### 已知限制

- 本版本仍需在真实飞牛 fnOS 图形界面完成远端镜像安装验证。
- Docker 默认停止宽限只有 10 秒；推荐设置 120 秒停止超时。
- 阿里云盘 Web 私有接口属于实验能力，可能因上游接口变化或风控策略失效。
- SQLite 部署仍只支持一个 Scheduler 和一个 Worker，Worker 并发度固定为 1。

## [0.2.0-rc.2] - 2026-07-28

这是 v0.2 可靠性基础的第二个候选版本，重点统一容器镜像，同时继续保持 API、Scheduler、Worker 等进程的职责边界。

### 改进

- 使用一个 OCI 镜像运行 API、Scheduler、Worker、数据库迁移、切换检查和 Web 前端六个服务。
- 同一次构建同时发布到 GHCR 和 Docker Hub，支持 `linux/amd64` 与 `linux/arm64`。
- Docker Compose 支持通过 `MEDIASYNC_IMAGE`、`MEDIASYNC_IMAGE_TAG` 和 `MEDIASYNC_HTTP_PORT` 配置镜像、版本及访问端口。
- Web 前端增加容器健康检查，Nginx 访问日志和错误日志直接输出到容器标准输出。
- 保留 API、Scheduler、Worker 独立进程，不使用进程管理器在单容器内混合运行。

### 兼容性

- 本版本不包含数据库模型、迁移或业务行为变化。
- 从 `v0.2.0-rc.1` 升级时，使用新的单镜像 Compose 配置重新拉取并启动服务即可。
- `v0.2.0-rc.1` 的前后端双镜像已同步至 Docker Hub，便于现有部署继续使用。

### 已知限制

- 本版本仍需继续完成长期稳定性观察。
- 阿里云盘 Web 私有接口属于实验能力，可能因上游接口变化或风控策略失效。
- SQLite 部署仍只支持一个 Worker，Worker 并发度固定为 1。

## [0.2.0-rc.1] - 2026-07-24

这是 v0.2 可靠性基础的首个候选版本，目标是在 NAS 上长期、无人值守运行。

### 新增

- API、Scheduler、Worker 独立进程拓扑。
- Task Engine v2：任务执行记录、原子领取、租约、心跳、恢复和锁令牌防护。
- 进程切换前数据库迁移与旧任务对账屏障。
- 阿里云盘私有接口扫码登录、账号编辑、OpenAPI 绑定和多目标盘目录选择。
- 分享订阅增量扫描、目录检查点、完整校验和请求频率保护。
- 文件记录、任务历史、运行日志和转存状态展示。

### 改进

- 转存任务幂等、失败重试、指数退避和远端结果对账。
- 分享订阅删除时正确处理关联任务历史。
- 文件记录页面的信息层级、状态说明和时间显示。
- 后端 UTC 时间在前端按浏览器本地时区显示。
- 项目架构文档、ADR、协作规范和运维手册统一为中文。

### 部署

- Docker Compose 默认运行单 API、单 Scheduler、单 Worker。
- 提供 `linux/amd64` 和 `linux/arm64` 的 GHCR 预构建镜像。
- SQLite 部署只支持一个 Worker，Worker 并发度固定为 1。

### 已知限制

- 本版本为候选版本，仍需完成八周稳定性观察。
- 阿里云盘 Web 私有接口属于实验能力，可能因上游接口变化或风控策略失效。
- Provider SDK v2、多云盘、多用户和 PostgreSQL 不在本版本范围内。

[0.2.0-rc.21]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.21
[0.2.0-rc.20]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.20
[0.2.0-rc.19]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.19
[0.2.0-rc.18]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.18
[0.2.0-rc.17]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.17
[0.2.0-rc.16]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.16
[0.2.0-rc.15]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.15
[0.2.0-rc.14]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.14
[0.2.0-rc.13]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.13
[0.2.0-rc.12]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.12
[0.2.0-rc.11]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.11
[0.2.0-rc.10]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.10
[0.2.0-rc.9]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.9
[0.2.0-rc.8]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.8
[0.2.0-rc.7]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.7
[0.2.0-rc.6]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.6
[0.2.0-rc.5]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.5
[0.2.0-rc.4]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.4
[0.2.0-rc.3]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.3
[0.2.0-rc.2]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.2
[0.2.0-rc.1]: https://github.com/josephyin/MediaSync/releases/tag/v0.2.0-rc.1
