# Docker 单容器部署

本文面向希望直接从 Docker Hub 拉取镜像并运行 MediaSync 的普通用户。默认
Appliance 模式使用一个 Web 端口、一个持久化目录和 Docker Socket，不需要下载
源码或编写 Compose 文件。

## 1. 部署契约

默认容器内部运行：

```text
MediaSync Launcher
├── Nginx（容器端口 9090）
├── API
├── Scheduler
└── Worker
```

这些职责仍是独立操作系统进程。Launcher 只负责迁移、对账、启停、进程监管和
健康汇总，不执行扫描、转存或 Provider 业务。

必须持久化：

```text
宿主机 MediaSync 数据目录 → /data
```

`/data` 同时保存 SQLite 数据库和运行时密钥。不要只备份数据库文件。

## 2. 首次安装

先在宿主机创建数据目录。下面的路径只是示例，请替换为 NAS 上真实且可写的
目录：

```bash
mkdir -p /volume1/docker/mediasync
```

拉取并启动精确版本：

```bash
docker pull josephyjq/mediasync:v0.2.0-rc.31

docker run -d \
  --name mediasync \
  -p 9090:9090 \
  -v /volume1/docker/mediasync:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  --stop-timeout 120 \
  josephyjq/mediasync:v0.2.0-rc.31
```

打开：

```text
http://NAS_IP:9090
```

管理员用户名和密码默认均为 `admin`。该默认值仅用于降低 NAS 图形界面的首次
安装门槛，首次登录后必须改为自己的强密码。

建议在首次安装时直接指定自己的密码：

```bash
docker run -d \
  --name mediasync \
  -p 9090:9090 \
  -v /volume1/docker/mediasync:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e ADMIN_PASSWORD='请替换为强密码' \
  --restart unless-stopped \
  --stop-timeout 120 \
  josephyjq/mediasync:v0.2.0-rc.31
```

显式密码不会写入日志。Shell 历史可能记录命令，介意时请使用环境变量文件或
NAS 的环境变量表单。

项目同时提供单容器模板 `compose.appliance.yml`。下载该文件后可以通过环境变量
指定数据目录和精确镜像：

```bash
MEDIASYNC_DATA_PATH=/volume1/docker/mediasync \
MEDIASYNC_IMAGE=josephyjq/mediasync:v0.2.0-rc.31 \
docker compose -f compose.appliance.yml up -d
```

该模板与上面的默认 `docker run` 命令一致，会挂载 Docker Socket。它不同于面向
高级多进程部署的 `docker-compose.yml`；内置一键更新目前只支持单容器模板。

## 3. 常用配置

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `ADMIN_USERNAME` | `admin` | 管理员用户名 |
| `ADMIN_PASSWORD` | `admin` | 管理员密码；首次登录后必须改为强密码 |
| `IMAGE_DEFAULT_ADMIN_ONLY` | `true` | 仅让镜像默认密码作用于新数据目录 |
| `SESSION_COOKIE_SECURE` | `false` | HTTPS 反向代理部署时设为 `true` |
| `LOG_LEVEL` | `INFO` | 容器日志级别 |
| `ALIYUNDRIVE_MODE` | `private_api` | 阿里云盘 Provider 模式 |

普通首次安装不需要手工配置 `SECRET_KEY` 和
`CREDENTIAL_ENCRYPTION_KEY`。Appliance 会安全生成并持久化它们。

镜像默认的 `admin` 密码只用于全新的 `/data`。已有数据目录中保存的管理员
密码不会在升级时被默认值覆盖；显式填写其他 `ADMIN_PASSWORD` 仍会执行离线
密码重置。

不要在已有数据库的情况下删除 `/data/config/runtime-secrets.json`。如果从旧
版本迁移数据库且该文件还不存在，第一次启动必须同时提供原来的
`SECRET_KEY`、`CREDENTIAL_ENCRYPTION_KEY` 和管理员密码。

## 4. 健康与日志

查看容器状态：

```bash
docker inspect \
  --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}无健康检查{{end}}' \
  mediasync
```

健康检查同时确认 Launcher、Nginx、API、Scheduler 和 Worker。健康输出示例：

```json
{"launcher":true,"nginx":true,"api":true,"scheduler":true,"worker":true}
```

查看日志：

```bash
docker logs --tail 200 mediasync
docker logs -f mediasync
```

任一关键进程异常退出时，Launcher 会停止其余进程并让容器非零退出。配合
`--restart unless-stopped`，Docker 会整体重启容器。

## 5. 备份与恢复

SQLite 运行期间可能存在 `-wal` 和 `-shm` 文件。最稳妥的备份方式是先停止
容器，再备份整个宿主机数据目录：

```bash
docker stop --timeout 120 mediasync
tar -C /volume1/docker -czf mediasync-backup.tar.gz mediasync
docker start mediasync
```

恢复时：

1. 停止并删除旧容器；
2. 把完整备份恢复到同一个宿主机目录；
3. 确认目录包含数据库和 `config/runtime-secrets.json`；
4. 用相同的目录映射重新创建容器；
5. 验证云盘账号可以正常校验。

## 6. 升级

### 6.1 通过容器管理器手动升级

候选版本建议使用精确标签，不要依赖浮动标签：

```bash
docker pull josephyjq/mediasync:新版本
docker stop --timeout 120 mediasync
docker rm mediasync
```

然后用首次安装时相同的端口、环境变量和宿主机目录创建新容器。默认入口会先
执行数据库迁移和旧任务对账，成功后才启动 Worker。

升级前必须备份整个数据目录。新旧容器不得同时访问同一个 SQLite 数据库。

### 6.2 实验性一键更新

官方单容器新安装命令默认挂载 Docker Socket，因此在“系统设置”中可以检查并
安装新版本：

```bash
-v /var/run/docker.sock:/var/run/docker.sock
```

不要移除 `/data` 映射、`--restart unless-stopped` 或
`--stop-timeout 120`。

Docker Socket 会让 MediaSync 获得接近宿主机 Docker 管理员的权限，因此：

- 默认安装会挂载 Socket，安装前必须知晓这等同于 Docker 管理员权限；
- 只允许可信的 MediaSync 官方镜像使用该 Socket；
- 不要把管理页面直接暴露到公网；
- 点击更新前仍应备份完整 `/data`；
- 页面显示“无法连接 Docker daemon”时，先确认 Socket 已映射且容器有权访问。

一键更新会拉取并校验精确镜像摘要，等待当前任务结束，再由临时助手切换容器；
候选容器未通过健康验证时会自动回滚。容器切换期间页面会短暂断开并自动重连。

如果更重视最小权限，可以从启动命令中删除 Docker Socket 映射。订阅、扫描、
转存和日志功能不受影响，但一键更新会关闭，需要继续通过容器管理器手动升级。

## 7. 从 v0.2.0-rc.2 Compose 升级

1. 停止 rc.2 的全部 Compose 服务；
2. 备份 `mediasync-data` volume 和 `.env`；
3. 把 volume 中的数据库文件复制到新的宿主机数据目录；
4. 第一次启动 rc.3 时传入 rc.2 `.env` 中原有的：
   - `SECRET_KEY`
   - `CREDENTIAL_ENCRYPTION_KEY`
   - `ADMIN_PASSWORD`
5. 等待容器健康；
6. 确认 `/data/config/runtime-secrets.json` 已创建；
7. 校验云盘账号和订阅；
8. 确认新容器正常后再清理旧 Compose 项目。

示例：

```bash
docker run -d \
  --name mediasync \
  -p 9090:9090 \
  -v /volume1/docker/mediasync:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e SECRET_KEY='rc.2 原值' \
  -e CREDENTIAL_ENCRYPTION_KEY='rc.2 原值' \
  -e ADMIN_PASSWORD='rc.2 原值' \
  --restart unless-stopped \
  --stop-timeout 120 \
  josephyjq/mediasync:v0.2.0-rc.31
```

密钥持久化成功后，后续重建容器可以不再重复传入两个加密密钥。

## 8. 回滚到 rc.2

1. 停止 rc.3 容器；
2. 备份当前完整 `/data`；
3. 在 rc.2 Compose 中恢复相同数据库；
4. 把 `runtime-secrets.json` 中对应的原始密钥值配置回 rc.2 `.env`；
5. 使用精确标签 `v0.2.0-rc.2` 启动；
6. 验证账号凭证和任务历史。

rc.3 没有新增数据库迁移，但回滚前仍必须保留完整备份。

## 9. 安全说明

- 默认用户名和密码均为 `admin`，首次登录后必须设置强密码；
- 默认 `SESSION_COOKIE_SECURE=false` 只适用于受信任的局域网 HTTP；
- 不要把 `9090` 管理端口直接映射到公网；
- 公网访问必须使用 HTTPS 反向代理，并设置
  `SESSION_COOKIE_SECURE=true`；
- 官方单容器新安装默认挂载 Docker Socket，它等同于 Docker 管理员权限；不需要
  一键更新时应删除该映射，MediaSync 不需要特权模式或额外 Linux capabilities；
- `/data/config/runtime-secrets.json` 包含敏感信息，不要公开或单独遗失；
- 私有接口 Provider 存在上游变更、限流和账号风控风险。

## 10. 常见问题

### 容器启动后立即退出

先看日志：

```bash
docker logs mediasync
```

常见原因是宿主机目录不可写、旧数据库缺少原密钥、密钥与已持久化值不一致、
迁移失败或端口冲突。

### 忘记管理员密码

用新的 `ADMIN_PASSWORD` 重建容器。Appliance 会原子更新持久化密码，但不会
把新密码写入日志。只重启没有改变环境变量的旧容器不会重置密码。

### 登录后修改管理员密码

默认单容器 Appliance 可以点击左下角管理员区域的锁形按钮在线修改密码。需要
输入当前密码和两次新密码；成功后全部旧会话失效，必须使用新密码重新登录。
新密码已经持久化到 `/data`，重启或重建容器时不会恢复旧值。

高级 Compose 的密码来源仍是宿主机 `.env`。界面会显示离线修改说明，修改
`ADMIN_PASSWORD` 后需要重建 API 容器。

### HTTPS 反向代理后仍无法登录

确认容器设置：

```text
SESSION_COOKIE_SECURE=true
```

同时让反向代理传递 `Host`、`X-Real-IP`、`X-Forwarded-For` 和
`X-Forwarded-Proto`。
