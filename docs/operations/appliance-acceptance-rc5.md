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

Tag 发布后继续确认：

- Docker Hub 与 GHCR 的 `v0.2.0-rc.5`、`rc` 多架构摘要一致；
- 未登录状态可以读取精确标签；
- 远程镜像包含 `8080/tcp`、`/data`、`ADMIN_PASSWORD=admin` 和
  `IMAGE_DEFAULT_ADMIN_ONLY=true`；
- Docker Hub 中文 Overview 已更新；
- rc.4 GitHub Release 已标记升级风险。
