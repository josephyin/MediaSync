# 群晖 DSM Container Manager 安装

本文说明如何在群晖 DSM 7 的 Container Manager 中安装 MediaSync。群晖不会
根据镜像声明自动创建宿主机文件映射，因此创建容器时必须手工添加 `/data`，
并按官方默认配置确认 Docker Socket 映射。

## 1. 准备数据目录

在 File Station 中创建专用目录，例如：

```text
/volume1/docker/mediasync
```

该目录保存 SQLite 数据库、运行时密钥和任务历史。确认容器对目录有读写权限。

## 2. 下载镜像

在 Container Manager 的“注册表”中搜索：

```text
josephyjq/mediasync
```

下载精确版本标签 `v0.2.0-rc.34`，不要使用来源不明的第三方镜像。

## 3. 创建容器

建议配置：

| 设置项 | 值 |
|---|---|
| 容器名称 | `mediasync` |
| 自动重新启动 | 开启 |
| 网络 | Bridge |
| 特权模式 | 关闭 |

端口只需要一条：

| 本地端口 | 容器端口 | 类型 |
|---:|---:|---|
| `9090` | `9090` | TCP |

## 4. 添加存储映射

在“存储空间设置”中点击“添加文件夹”，手工配置：

| 群晖文件夹 | 装载路径 | 权限 |
|---|---|---|
| `/volume1/docker/mediasync` | `/data` | 读写 |
| `/var/run/docker.sock` | `/var/run/docker.sock` | 读写 |

镜像虽然声明了容器路径 `/data`，但群晖创建界面不会自动带出宿主机文件夹。
如果跳过这一步，Docker 会创建随机名称的匿名卷；容器可以启动，但重建时不会
自动复用，备份和迁移也很困难。

在启动容器前，务必确认配置页面能看到：

```text
/volume1/docker/mediasync → /data
/var/run/docker.sock → /var/run/docker.sock
```

不要把 `/data` 映射为只读，也不要在删除容器时同时删除对应数据。

Docker Socket 映射等同于授予 MediaSync 宿主机 Docker 管理员权限。它是官方
单容器新安装的推荐默认值，但群晖不会仅根据 Docker Hub 镜像自动带出该绑定，
必须由用户手工添加并确认。只使用可信官方镜像，不要把管理端口暴露到公网。
不需要一键更新时可以省略 Socket 映射，其他功能不受影响。

## 5. 环境变量

全新安装可以使用镜像默认值：

| 变量 | 默认值 |
|---|---|
| `ADMIN_PASSWORD` | `admin` |
| `IMAGE_DEFAULT_ADMIN_ONLY` | `true` |

建议首次创建时直接把 `ADMIN_PASSWORD` 改为强密码。已有 `/data` 升级时，
`IMAGE_DEFAULT_ADMIN_ONLY=true` 会阻止镜像默认的 `admin` 覆盖原密码。

启动并登录后，也可以点击管理后台左下角管理员区域的锁形按钮在线修改密码。
修改成功后所有旧会话立即失效，新密码随 `/data` 持久化。

## 6. 启动与检查

启动后访问：

```text
http://群晖_IP:9090
```

健康检查会验证 Launcher、Nginx、API、Scheduler 和 Worker。NAS 启动较慢时
镜像会保留 60 秒启动宽限，单次检查最多等待 15 秒。

若 Container Manager 长时间显示黄色：

1. 先确认页面能否打开；
2. 在容器“日志”中检查迁移、目录权限或进程退出错误；
3. 在“详情”中查看健康检查最近一次输出；
4. 确认没有覆盖镜像默认命令。

页面正常但旧版本仍提示 `Health check exceeded timeout (5s)` 时，请升级到包含
NAS 健康检查修复的新版本。

## 7. 升级

按第 4 节配置 Docker Socket 后，可以在 MediaSync“系统设置”中使用实验性一键
更新。页面会等待任务结束、校验精确镜像并在候选失败时自动回滚。

也可以继续使用 Container Manager 手动升级。不需要一键更新时删除：

| 群晖文件 | 装载路径 | 权限 |
|---|---|---|
| `/var/run/docker.sock` | `/var/run/docker.sock` | 读写 |

删除 Socket 后，在 MediaSync“系统设置”中会显示手动升级指引。无论使用哪种
方式，更新前都必须备份完整 `/data`。

手动升级步骤：

1. 停止容器；
2. 备份整个 `/volume1/docker/mediasync`；
3. 下载新的精确镜像标签；
4. 删除旧容器，但保留宿主机数据目录；
5. 使用相同的 `/data` 映射、端口和环境变量创建新容器；
6. 等待健康状态并登录校验账号与订阅。

新旧容器不得同时挂载同一个 `/data`。
