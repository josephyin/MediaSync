# MediaSync

> 自托管的云盘影音同步服务。

MediaSync 是一个通用的家庭影音云盘订阅同步工具。它定时检查资源分享目录，将新增文件自动转存到个人云盘，并可配合 OpenList、SmartStrm、MoviePilot、Emby、Jellyfin 和飞牛影视构建自动化影音库。

```text
资源分享 → MediaSync → 个人云盘 → STRM 生成 → 媒体库整理 → 播放器
```

## 云盘服务支持

- ✅ 阿里云盘（MVP Provider；私有接口实验模式）
- ⬜ 夸克网盘
- ⬜ 115
- ⬜ OneDrive

> 当前候选版本为 `v0.2.0-rc.13`。普通用户可以用一个容器直接运行，并可在系统设置中使用实验性一键更新；API、Scheduler、Worker 和 Nginx 在容器内仍是职责独立的进程。版本仍处于稳定性观察期，默认 Web 私有接口可能随上游更新失效。

## MVP 功能

- Web 管理后台和单管理员认证
- 云盘账号管理、编辑、本地扫码登录及凭证加密
- 分享订阅、首次同步策略和定时检查
- 订阅级目标盘选择（默认盘/资源库/备份盘/自定义 Drive ID）与目录浏览
- 基于远端文件 ID/指纹的增量检测
- 幂等转存任务和失败重试
- 文件记录、转存历史和任务日志
- Provider 注册机制
- 实验性 Web 一键镜像更新、任务排空、健康验证和失败回滚
- 单容器 Docker 部署与高级 Docker Compose 部署

## 技术栈

- 后端：Python 3.12、FastAPI、SQLAlchemy、SQLite、APScheduler、Pydantic
- 前端：Vue 3、Vite、TypeScript、Element Plus
- 部署：Docker、Docker Compose、Nginx

## 快速开始

### Docker 单容器（推荐）

```bash
mkdir -p /你的路径/mediasync

docker run -d \
  --name mediasync \
  -p 9090:9090 \
  -v /你的路径/mediasync:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  --stop-timeout 120 \
  josephyjq/mediasync:v0.2.0-rc.13
```

访问 `http://NAS_IP:9090`，默认管理员用户名和密码均为 `admin`。首次登录后
应立即点击左下角管理员区域的锁形按钮修改为强密码；不要把使用默认密码的管理
端口暴露到公网。

也可以在首次启动时直接指定密码：

```bash
docker run -d \
  --name mediasync \
  -p 9090:9090 \
  -v /你的路径/mediasync:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e ADMIN_PASSWORD='你的强密码' \
  --restart unless-stopped \
  --stop-timeout 120 \
  josephyjq/mediasync:v0.2.0-rc.13
```

数据库和运行时密钥都保存在宿主机映射的 `/你的路径/mediasync` 中，备份和恢复时
必须把整个目录作为一个整体。详细说明见
[Docker 单容器部署](docs/deployment/docker-run.md)；飞牛用户见
[飞牛 fnOS 安装教程](docs/deployment/fnos.md)，群晖用户见
[群晖 DSM 安装教程](docs/deployment/synology.md)。

> 默认配置适用于局域网 HTTP，不要把管理端口直接暴露到公网。通过 HTTPS
> 反向代理访问时，请增加 `-e SESSION_COOKIE_SECURE=true`。Docker Socket 等同于
> 宿主机 Docker 管理员权限；不需要一键更新时可以删除该映射，其他功能不受影响。

### Docker Compose（高级）

需要职责级容器隔离、开发调试或显式控制每个进程时，可以继续使用官方 Compose：

```bash
cp .env.example .env
```

编辑 `.env`，至少替换密钥和管理员密码，并按需使用预构建镜像：

```dotenv
SECRET_KEY=一个足够长的随机字符串
CREDENTIAL_ENCRYPTION_KEY=另一个足够长的随机字符串
ADMIN_PASSWORD=强密码
MEDIASYNC_IMAGE=ghcr.io/josephyin/mediasync
MEDIASYNC_IMAGE_TAG=v0.2.0-rc.13
```

```bash
docker compose pull
docker compose up -d --no-build
```

源码构建仍可使用 `docker compose up -d --build`。Appliance 与 Compose 不得同时
访问同一个 SQLite 数据库。

### 本地开发

后端：

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp ../.env.example .env
alembic upgrade head
uvicorn app.main:app --reload
```

前端：

```bash
cd frontend
npm install
npm run dev
```

开发地址为 `http://localhost:5173`，Vite 会把 `/api` 代理到 `http://localhost:8000`。

## 项目结构

```text
backend/       FastAPI、SQLAlchemy、Task Engine、Provider、Appliance 和测试
frontend/      Vue 3 管理后台
deploy/        Nginx 配置
docs/          架构、部署和开发文档
docker-compose.yml
```

完整设计见 [MVP 设计方案](docs/mvp-design.md)。

## 安全说明

- 私有/Open refresh token、Open Client Secret 和分享密码使用 `CREDENTIAL_ENCRYPTION_KEY` 加密后存入 SQLite。
- 私有接口模式支持在 MediaSync 本机生成阿里云盘登录二维码；扫码确认后 token 由后端直接加密保存，不经过第三方取 token 服务。
- 管理 API 使用签名的 HttpOnly Cookie；公网部署必须在反向代理层启用 HTTPS。
- Appliance 默认使用局域网 HTTP Cookie；HTTPS 部署必须设置 `SESSION_COOKIE_SECURE=true`。
- 不要提交 `.env`、数据库、日志或真实第三方凭证。
- 修改 `CREDENTIAL_ENCRYPTION_KEY` 会导致已有凭证无法解密。
- `/data/mediasync.db*` 和 `/data/config/runtime-secrets.json` 是不可分割的备份单元。
- `private_api` 会模拟 Aliyun Drive Web 客户端调用未公开接口，存在接口变更、限流和账号风控风险；请只操作自己有权访问的账号与分享内容。

## Provider 开发

业务代码只依赖 `CloudDriveProvider`。新 Provider 应实现账号校验、分享解析/遍历、目标目录解析/创建、目标文件查重和分享转存能力，并在注册表中声明。

阿里云盘提供私有接口和 OpenAPI 两个适配器：

- `ALIYUNDRIVE_MODE=private_api`（默认、实验性）：调用 Web 私有接口，实现 refresh token 刷新、分享解析与分页、个人盘目录查询/创建和分享转存。无需开放平台 Client ID，但不承诺接口稳定性。
- `ALIYUNDRIVE_MODE=official`：调用 `https://openapi.alipan.com`，需要配置开放平台应用的 `ALIYUNDRIVE_CLIENT_ID` 和 `ALIYUNDRIVE_CLIENT_SECRET`；当前只支持账号校验和个人盘目录操作。

两种实现都遵循同一个 `CloudDriveProvider` 契约。在默认 `private_api` 模式中，可以给同一账号额外绑定一套 OpenAPI 凭证：私有 token 负责分享读取和转存，Open token 负责读取完整的默认盘、资源库和备份盘信息。绑定时会比较两套凭证返回的 `user_id`，防止跨账号转存。

OpenAPI 绑定支持：

- AListGo 托管刷新：填写 Open refresh token 和可配置的 HTTPS Token URL。该模式会把 Open token 发送给对应服务，界面会明确提示风险。
- OpenList APIPages：填写由 OpenList APIPages 签发的阿里云盘 OAuth2 refresh token，默认使用国内社区端点，也可切换全球站或自建 APIPages 地址。
- 自有应用：填写由自己 OpenAPI 应用签发的 refresh token、Client ID 和 Client Secret，MediaSync 直接请求阿里 Open OAuth，不经过第三方 Token 服务。

不同应用签发的 refresh token 不能互换。AListGo、OpenList APIPages 和自有应用的 Open token 必须搭配各自的刷新服务或 Client 凭据；它们不能作为 Web 私有 token 使用，私有扫码取得的 token 也不能直接请求 OpenAPI。

在 `private_api` 模式下，推荐在“云盘账号”页面选择“扫码添加”，使用阿里云盘 App 扫码并在手机上确认。已有账号可以使用“重新扫码”安全更新 token。删除仍被订阅使用的账号时，MediaSync 会阻止删除并提示先移除关联订阅，以免产生孤立任务记录。

创建订阅时先选择“目标盘”，再打开目标目录选择器。绑定 OpenAPI 后，MediaSync 会自动合并默认盘、资源库和备份盘；未绑定且私有接口只返回默认盘时，仍可直接粘贴 Drive ID。目标盘保存在订阅上，因此同一账号的不同订阅可以写入不同的盘。

## 扫描策略与请求保护

MediaSync 使用目录检查点降低日常扫描的请求量：

- 首次扫描执行完整递归，并为每个子目录建立检查点。
- 常规轮询始终检查分享根目录，再检查最久未扫描的一批子目录，默认每轮最多 20 个。
- 默认每 24 小时执行一次完整递归校验，用于发现目录移动、删除等轮询阶段可能遗漏的变化。
- Web 页面允许手动触发“完整校验”，但会先提示 API 请求量风险。

扫描结果仍用 `subscription_id + remote_file_id` 与本地文件索引做增量判断，不会重复创建已经见过的文件或转存任务。通常每个被检查目录至少产生一次请求，每超过 200 个项目还会增加分页请求。任务日志会记录扫描模式、目录数、检查点数和实际 API 请求次数。

私有接口模式默认启用以下保护：

- 所有阿里云盘私有 API 请求全局串行，间隔至少 0.8 秒并增加 0–0.3 秒随机抖动。
- 遇到 `429` 或可恢复的 `5xx` 响应时遵循 `Retry-After`，否则指数退避，最多重试 3 次。
- 创建目录、执行转存等写操作不做 HTTP 自动重放，避免超时后重复创建。
- 定时任务随机错峰最多 120 秒，同一订阅禁止并发扫描，最短扫描周期为 15 分钟；手动扫描默认有 60 秒冷却时间。
- 转存每批最多处理 2 个；失败后从 30 秒开始延迟重试，最长退避 15 分钟。

这些机制只能降低请求密度和瞬时并发，不能保证私有接口永不触发平台风控。大目录建议使用 30 分钟或更长的扫描周期，不要反复手动执行完整校验；相关参数可通过 `.env` 中的 `FOLDER_SCAN_BATCH_SIZE` 和 `FULL_SCAN_INTERVAL_HOURS` 调整。

## 路线图

- [x] 实现阿里云盘私有接口分享目录与分享转存链路
- [x] 完成 `v0.1` 功能 MVP
- [x] 发布 `v0.2.0-rc.1` 可靠性基础预发布版
- [x] 发布 `v0.2.0-rc.2` 单镜像部署预发布版
- [x] 发布 `v0.2.0-rc.6` NAS 健康检查与品牌图标预发布版
- [x] 发布 `v0.2.0-rc.7` 默认端口 `9090` 预发布版
- [x] 发布 `v0.2.0-rc.8` 工程质量与运维能力预发布版
- [x] 发布 `v0.2.0-rc.9` 任务执行信息修复预发布版
- [x] 发布 `v0.2.0-rc.10` Updater 恢复协调器演练候选版
- [x] 发布 `v0.2.0-rc.11` Web 一键更新候选版
- [x] 标记 `v0.2.0-rc.12` Web 会话过期跳转修复候选版（镜像构建取消）
- [x] 发布 `v0.2.0-rc.13` 多架构构建修复候选版
- [ ] 完成 v0.2 八周稳定性观察
- [ ] 发布 `v0.2.0` 正式版
- [ ] 夸克网盘 Provider
- [ ] 115 Provider
- [ ] OneDrive Provider
- [ ] 多用户和更细粒度权限
- [ ] PostgreSQL 与分布式任务队列

## 参与贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。请勿在代码、测试、Issue 或日志中提交真实 token 和 Cookie。

## 许可证

[MIT](LICENSE)
