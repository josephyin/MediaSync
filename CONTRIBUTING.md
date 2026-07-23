# Contributing to MediaSync

感谢你参与 MediaSync。

## 开发流程

1. Fork 仓库并从 `dev` 创建功能分支。
2. 后端代码放在 `backend/app`，第三方云盘逻辑必须放在 `providers` 中。
3. 新 Provider 需要实现统一 Provider 协议并提供 mock 测试。
4. 不得在提交、日志、测试夹具或 issue 中包含真实 token、Cookie 和分享密码。
5. 提交前运行后端测试和前端构建。

## Commit

建议使用 Conventional Commits，例如：

```text
feat(provider): add Aliyun Drive folder listing
fix(scan): avoid duplicate transfer tasks
docs: update Docker deployment guide
```
