# v0.2.0-rc.5 管理员密码升级保护验收记录

- 日期：2026-07-28
- 目标版本：v0.2.0-rc.5
- 变更范围：镜像默认管理员密码的首次初始化与升级语义

## 验收不变量

- 全新 `/data` 使用镜像默认 `admin/admin`；
- 已有 `/data` 不被镜像默认密码覆盖；
- 用户显式设置其他 `ADMIN_PASSWORD` 时仍可重置；
- 签名密钥和凭证加密密钥不受影响；
- 飞牛表单继续显示单个 `8080`、`/data` 和默认管理员密码。

## 自动化验证

- Ruff 通过；
- 后端全量测试通过；
- 运行时密钥与 Compose 契约专项测试通过；
- 前端生产构建通过；
- `uv lock --check` 通过；
- 单镜像构建通过。

## 发布后验证

发布工作流已成功完成：

- GitHub Actions：
  <https://github.com/josephyin/MediaSync/actions/runs/30332061720>；
- Docker Hub 与 GHCR 的 `v0.2.0-rc.5`、`rc` 标签均指向摘要
  `sha256:7ebe99b63d01ffdb9dd44df0de3dc17cac1783692546dec176abc7dfc1d59e62`；
- 两个镜像仓库均包含 `linux/amd64` 和 `linux/arm64`；
- 使用未登录 Docker 客户端读取两个仓库的精确标签成功；
- 从 Docker Hub 拉取的远程镜像仅暴露 `8080/tcp`，声明 `/data` 数据卷，
  并包含 `ADMIN_PASSWORD=admin` 和 `IMAGE_DEFAULT_ADMIN_ONLY=true`；
- Docker Hub 中文 Overview 已更新为 rc.5；
- rc.4 GitHub Release 已标记升级风险，并引导用户升级 rc.5。

## 远程镜像升级演练

使用 Docker Hub 发布镜像 `josephyjq/mediasync:v0.2.0-rc.5` 完成以下演练：

1. 使用全新数据卷和自定义管理员密码启动，容器健康；
2. 自定义密码登录返回 HTTP 200，默认密码登录返回 HTTP 401；
3. 保留同一数据卷，移除显式 `ADMIN_PASSWORD` 后重新创建容器；
4. 原自定义密码登录仍返回 HTTP 200，默认密码登录仍返回 HTTP 401；
5. 演练结束后已删除临时容器和数据卷。

验证结果表明：镜像默认密码只用于全新数据目录，不会在升级时覆盖已有密码。

## 待完成验收

- 在真实飞牛 fnOS 镜像创建表单中确认默认显示一个 `8080:8080` 端口映射、
  容器路径 `/data`（本地路径待用户选择）以及 `ADMIN_PASSWORD=admin`。
