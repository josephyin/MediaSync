# 容器镜像发布手册

MediaSync 的容器镜像通过 GitHub Actions 从同一次构建同时发布到 GitHub
Container Registry（GHCR）和 Docker Hub。

## 镜像

从 v0.2.0-rc.2 开始，每个版本只使用一个 `mediasync` 镜像。v0.2.0-rc.3
开始，镜像默认使用单容器 Appliance 入口；迁移、对账、API、Scheduler、Worker
和 Nginx 的显式命令继续用于高级多容器部署。

v0.2.0-rc.1 是双镜像历史版本：

- `mediasync-backend`
- `mediasync-frontend`

所有镜像都发布 `linux/amd64` 和 `linux/arm64`。

## GitHub Secrets

仓库需要配置以下 Actions Secrets：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名或组织名；
- `DOCKERHUB_TOKEN`：具有目标仓库镜像读写权限的 Docker Hub Access Token。

Access Token 只能保存在 GitHub Secrets 中，不得写入仓库、Issue、PR、日志或
聊天内容。

Docker Hub 对镜像推送和仓库详情更新采用不同的权限校验。Token 即使能够推送
镜像，也可能无权更新 Repository Overview。需要自动同步 Overview 时，应给
Token 配置目标仓库的详情管理权限；否则工作流会保留警告，但不会因为说明同步
失败而把已经成功的镜像发布标记为失败。维护者随后可以在 Docker Hub 仓库页面
手动复制 `docs/dockerhub-overview.md`。

## 新版本发布

推送 `v*` 标签后，`发布容器镜像` 工作流会从同一源码构建一次，并把相同产物
同时推送到 GHCR 和 Docker Hub。

候选版本会额外更新 `rc` 标签；正式版本会额外更新 `latest` 标签。已经发布的
精确版本标签不得覆盖。

## 同步既有版本

对于已经存在于 GHCR、但尚未进入 Docker Hub 的单镜像版本，在 GitHub Actions
页面手动运行 `发布容器镜像`，填写已有标签，例如：

```text
v0.2.0-rc.23
```

手动同步不会重新构建镜像。工作流会复制 GHCR 中的多架构 OCI 索引到 Docker
Hub，避免改变既有版本的代码或依赖。

## 验证

同步完成后，在未登录 Docker Hub 的环境检查两个多架构清单：

```bash
docker manifest inspect <用户名>/mediasync:v0.2.0-rc.23
```

清单必须同时包含：

- `linux/amd64`
- `linux/arm64`

## Docker Hub Overview

发布工作流会尝试把 `docs/dockerhub-overview.md` 同步到 Docker Hub。该步骤
属于发布后的说明同步，不是镜像产物的一部分：

- 同步成功：无需人工处理；
- 同步失败：工作流输出警告，镜像发布结果保持成功；
- 手动回退：登录 Docker Hub，打开仓库 `General` 页面，编辑
  `Repository overview` 并复制该文档内容。

不要为了修复 Overview 而移动、覆盖或重新推送已经发布的精确版本标签。
