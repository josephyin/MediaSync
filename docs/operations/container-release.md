# 容器镜像发布手册

MediaSync 的容器镜像通过 GitHub Actions 同时发布到 GitHub Container
Registry（GHCR）和 Docker Hub。

## 镜像

v0.2.0-rc.1 使用两个镜像：

- `mediasync-backend`：供迁移、对账、API、Scheduler 和 Worker 复用；
- `mediasync-frontend`：Nginx 和 Vue 管理后台。

每个版本同时发布 `linux/amd64` 和 `linux/arm64`。

## GitHub Secrets

仓库需要配置以下 Actions Secrets：

- `DOCKERHUB_USERNAME`：Docker Hub 用户名或组织名；
- `DOCKERHUB_TOKEN`：具有目标仓库读写权限的 Docker Hub Access Token。

Access Token 只能保存在 GitHub Secrets 中，不得写入仓库、Issue、PR、日志或
聊天内容。

## 新版本发布

推送 `v*` 标签后，`发布容器镜像` 工作流会从同一源码构建一次，并把相同产物
同时推送到 GHCR 和 Docker Hub。

候选版本会额外更新 `rc` 标签；正式版本会额外更新 `latest` 标签。已经发布的
精确版本标签不得覆盖。

## 同步既有版本

对于工作流升级前已经存在于 GHCR 的版本，在 GitHub Actions 页面手动运行
`发布容器镜像`，填写已有标签，例如：

```text
v0.2.0-rc.1
```

手动同步不会重新构建镜像。工作流会复制 GHCR 中的多架构 OCI 索引到 Docker
Hub，避免改变既有版本的代码或依赖。

## 验证

同步完成后，在未登录 Docker Hub 的环境检查两个多架构清单：

```bash
docker manifest inspect <用户名>/mediasync-backend:v0.2.0-rc.1
docker manifest inspect <用户名>/mediasync-frontend:v0.2.0-rc.1
```

清单必须同时包含：

- `linux/amd64`
- `linux/arm64`
