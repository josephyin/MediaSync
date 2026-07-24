# 参与 MediaSync 开发

感谢你参与 MediaSync。

## 语言规范

MediaSync 面向维护者和用户的内容统一使用简体中文，包括：

- README、架构设计、ADR、迁移说明和运维手册；
- Issue、PR、Milestone、Release Note 和评审意见；
- Commit 的主题与正文；
- 面向用户的配置说明、错误解释和界面文案。

代码标识、API 字段、数据库字段、状态枚举、命令、日志事件名和第三方协议专有名词
保留英文，以保证兼容性和可检索性。引用英文资料时，应同时提供中文说明。

## 开发流程

1. Fork 仓库并从 `dev` 创建功能分支。
2. 后端代码放在 `backend/app`，第三方云盘逻辑必须放在 `providers` 中。
3. 新 Provider 需要实现统一 Provider 协议并提供 mock 测试。
4. 不得在提交、日志、测试夹具或 issue 中包含真实 token、Cookie 和分享密码。
5. 提交前运行后端测试和前端构建。

## 架构治理

MediaSync 采用 **不变量优先（Invariant First）** 和
**运行时代码之前先完成设计（Design Before Runtime）**：

```text
设计 PR
    ↓
评审
    ↓
合并架构设计
    ↓
Issue
    ↓
运行时 PR
```

修改以下核心模块的不变量前，必须先提交并合并对应的设计 PR：

- Task Engine
- Provider
- Credential
- Scheduler
- Storage

以下变化视为触发条件：

- 数据模型、持久化语义或迁移兼容性变化。
- 状态机、生命周期或终态语义变化。
- Provider 或 Storage contract 变化。
- Scheduler、Worker 或任务所有权边界变化。
- Credential、安全、加密或认证模型变化。
- 支持的部署拓扑或基础设施依赖变化。

设计 PR 必须说明状态、约束、生命周期、失败恢复、迁移兼容性和非目标。
运行时 PR 必须关联已合并的设计文档或 ADR，并保持 Issue 约定的边界。
不得在数据模型 PR 中顺带实现 Worker、Scheduler 或 Provider 行为。

详细原则见
[架构原则](docs/architecture/principles.md)，决策记录流程见
[ADR 指南](docs/architecture/adr/README.md)。

## 交付与合并

项目按 `里程碑 → Issue → PR` 组织工作，版本号用于里程碑完成后的
发布标签，不用于扩大单个 PR 的范围。

每个运行时 PR 应：

1. 写明它保证的架构不变量。
2. 只验证当前层次的责任：
   - 数据不变量：模型、约束、迁移和历史数据。
   - 引擎行为：任务领取、租约、心跳、恢复和 fencing。
   - 生产运行时：重启、断网、凭证失效和远端部分成功。
3. 提供与该层次匹配的不变量测试。
4. 不修改未在 Issue 和设计 PR 中授权的核心行为。

README 应反映已经发布的能力。路线和设计变化先记录在里程碑、
Issue、设计规范或 ADR 中，在发布准备阶段统一更新 README。

## 提交信息

提交信息使用 Conventional Commits 格式，说明文字使用中文，例如：

```text
feat(provider): 增加阿里云盘目录读取
fix(scan): 避免重复创建转存任务
docs: 更新 Docker 部署指南
```
