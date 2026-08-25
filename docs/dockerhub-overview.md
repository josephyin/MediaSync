# MediaSync

自托管的家庭影音云盘订阅同步服务。

MediaSync 定时检查资源分享目录，把新增文件增量转存到个人云盘，并可配合
OpenList、SmartStrm、MoviePilot、Emby、Jellyfin 和飞牛影视构建自动影音库。

## 支持状态

- ✅ 阿里云盘
- ✅ 夸克网盘（Cookie 私有接口，实验性）
- ⬜ 115
- ⬜ OneDrive

当前为候选版本，阿里云盘和夸克网盘 Web 私有接口属于实验能力，可能因上游变化
或账号风控失效。

## 一条命令启动

```bash
mkdir -p /你的路径/mediasync

docker run -d \
  --name mediasync \
  -p 9090:9090 \
  -v /你的路径/mediasync:/data \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --restart unless-stopped \
  --stop-timeout 120 \
  josephyjq/mediasync:v0.2.0-rc.18
```

打开 `http://NAS_IP:9090`，默认用户名和密码均为 `admin`。首次登录后必须改为
自己的强密码。

也可以首次启动时设置：

```text
ADMIN_PASSWORD=你的强密码
```

## 必须配置

| 类型 | 配置 |
|---|---|
| 端口 | 宿主机 `9090` → 容器 `9090` |
| 存储 | 宿主机数据目录 → 容器 `/data`，读写 |
| 一键更新 | `/var/run/docker.sock` → `/var/run/docker.sock`，读写 |
| 重启策略 | `unless-stopped` |

不需要特权模式，也不需要映射内部 API 端口 `8000`。Docker Socket 等同于宿主机
Docker 管理员权限，只应交给可信官方镜像；不需要一键更新时可以删除该映射，
其他功能不受影响。

## 数据与备份

`/data` 同时保存 SQLite 数据库和凭证密钥。停止容器后备份整个宿主机数据目录，
不要只备份数据库文件。

## HTTP 与 HTTPS

默认密码为 `admin`，`SESSION_COOKIE_SECURE=false`，仅适用于受信任的局域网
首次安装。请立即修改密码，不要把管理端口直接暴露到公网。

通过 HTTPS 反向代理访问时设置：

```text
SESSION_COOKIE_SECURE=true
```

## 健康检查

镜像健康检查同时确认 Launcher、Nginx、API、Scheduler 和 Worker。任一关键
进程异常退出时，容器会整体非零退出，再由 Docker 重启策略恢复。

## 更多文档

- GitHub：https://github.com/josephyin/MediaSync
- Docker 部署：https://github.com/josephyin/MediaSync/blob/main/docs/deployment/docker-run.md
- 飞牛 fnOS：https://github.com/josephyin/MediaSync/blob/main/docs/deployment/fnos.md
- 群晖 DSM：https://github.com/josephyin/MediaSync/blob/main/docs/deployment/synology.md
- 问题反馈：https://github.com/josephyin/MediaSync/issues

许可证：MIT
