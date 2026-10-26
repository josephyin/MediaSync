# 本地及 NAS 挂载目录下载

状态：未发布开发功能，不包含在 v0.2.3 中。本轮完成代码与自动化验证，
真实 PikPak 账号、上游下载链接和 NAS 文件系统验收暂缓，不能据此宣称线上可用。

文件记录中选择已转存的 PikPak 文件，点击“下载到本地”，或勾选多个文件点击“下载选中”。
下载写入运行 MediaSync 的服务器，不是访问网页的电脑。第一版仅支持 PikPak，最多一次选择 10,000 个文件。翻页保留手动勾选；“跨页全选下载”选择当前订阅、搜索和状态筛选下全部可下载文件，并在弹窗确认总数。选择完成后新增的文件不自动加入本次下载。
保留订阅内相对目录结构；不同订阅建议选择不同子目录，避免同名文件冲突。
同批选择或已有任务占用相同目标路径时明确拒绝；其他源文件的未完成分片不会被覆盖。
下载恢复文件使用的 `.mediasync.part`、`.mediasync.json` 等保留文件名不可作为源文件名。
任务中心选择“本地下载”可查看已下载字节、结果、取消和手动重试。

## 目录配置

在下载弹窗点击“选择下载目录”，可以浏览服务器目录、返回上一级，或输入绝对路径后点击“打开”。
点击“使用此目录”会保存默认位置，无需重启。仅管理员可浏览和修改。
每个任务保存提交时的绝对根目录，修改默认位置不会改变已排队任务的目标。
本地开发配置默认保存在 `./data/download-settings.json`；Appliance 与官方 Compose
默认保存在 `/data/config/download-settings.json`，可通过 `DOWNLOAD_SETTINGS_PATH` 调整；
请将该文件所在目录纳入持久化卷。页面配置优先于 `DOWNLOAD_ROOT` 环境默认值。


本地开发默认 `DOWNLOAD_ROOT=./data/downloads`；容器默认 `/data/downloads`。
目录必须预先存在；
不会自动创建根目录，避免 NAS 未挂载时在意外位置创建下载根目录。
挂载后请确保运行后端和 worker 的用户具有读写权限，且文件系统支持硬链接（用于原子发布且不覆盖）。
API 与 worker 必须配置同一个根目录及挂载。

本机例子：

```sh
mkdir -p /srv/downloads
export DOWNLOAD_ROOT=/srv/downloads
```

NAS：先用操作系统挂载 SMB/NFS，再把 `DOWNLOAD_ROOT` 指向该挂载点内的目录。
本功能不保存 SMB/NFS 账号密码，也不自动挂载网络共享。

Docker Compose 服务配置示例（替换宿主机路径，API 与 worker 都需映射）：

```yaml
environment:
  DOWNLOAD_ROOT: /downloads
volumes:
  - /mnt/nas/media:/downloads
```

## 完整性与恢复

- 下载地址在执行时获取，不存入数据库或日志；只接受 PikPak 官方 HTTPS 下载域名。
- 临时文件在目标目录内，完成大小及可用 PikPak GCID 内容 hash 校验后才原子发布。
  同目录保存私有恢复元数据和完成凭据，不包含 URL 或账号凭证；请一并保留。
  任务在原子发布后崩溃时，可按完成凭据核对本地文件并收尾，不重复下载或覆盖。
- 中断保留分片，下一次获取新链接后验证 Range 响应并继续下载；200 全量响应不追加
  到旧内容。大文件的上游不支持 Range 时明确失败；不会无限从头下载。
- 默认每段最多 32 MiB 或约 8 秒后释放 Worker；GCID 校验也按段处理。
  继续下载不消耗失败重试次数，队列中的扫描与转存仍可执行。网络超时另受 20 秒限制。
- `DOWNLOAD_RATE_LIMIT_KIB=0` 表示不限速，可设置正整数限制 KiB/s；
  `DOWNLOAD_CHUNK_BYTES=33554432` 调整每段上限。官方 Compose 会传入限速配置。
- 手动重试终态下载会创建后继任务，不改写原 Task/TaskRun；更新凭证后可以继续
  等待凭证的任务。取消和分段等待都保留已落盘的进度。
- 路径不能越过根目录或经过符号链接。空间不足、权限不足、NAS 断连等显示失败，修复后可手动重试。
- 支持 legacy 调度器和独立 Task Engine worker；`SCHEDULER_ENABLED=false` 的 legacy 部署不自动消费下载任务。
- 下载不修改云盘文件的转存状态，不会删除云端源文件。
- 下载期间重新登录时，新凭证优先；旧任务仅在原凭证未改变时保存轮换结果。

GCID 校验独立实现，协议字段与分块算法核对参考：
[PikPak 上传 hash 类型](https://github.com/OpenListTeam/OpenList/blob/main/drivers/pikpak/driver.go)、
[GCID 分块定义](https://github.com/OpenListTeam/OpenList/blob/main/pkg/utils/hash/gcid.go)。
