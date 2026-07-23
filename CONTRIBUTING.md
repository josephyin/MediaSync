# Contributing to MediaSync

感谢你参与 MediaSync。

## 开发流程

1. Fork 仓库并从 `dev` 创建功能分支。
2. 后端代码放在 `backend/app`，第三方云盘逻辑必须放在 `providers` 中。
3. 新 Provider 需要实现统一 Provider 协议并提供 mock 测试。
4. 不得在提交、日志、测试夹具或 issue 中包含真实 token、Cookie 和分享密码。
5. 提交前运行后端测试和前端构建。

## 架构治理

MediaSync 采用 **Invariant First** 和 **Design Before Runtime**：

```text
Design PR
    ↓
Review
    ↓
Architecture Merge
    ↓
Issue
    ↓
Runtime PR
```

修改以下核心模块的不变量前，必须先提交并合并对应的 Design PR：

- Task Engine
- Provider
- Credential
- Scheduler
- Storage

Design PR 必须说明状态、约束、生命周期、失败恢复、迁移兼容性和非目标。
Runtime PR 必须关联已合并的设计文档或 ADR，并保持 Issue 约定的边界。
不得在数据模型 PR 中顺带实现 Worker、Scheduler 或 Provider 行为。

详细原则见
[Architecture Principles](docs/architecture/principles.md)，决策记录流程见
[ADR Guide](docs/architecture/adr/README.md)。

## 交付与合并

项目按 `Milestone → Issue → PR` 组织工作，版本号用于 Milestone 完成后的
Release Tag，不用于扩大单个 PR 的范围。

每个 Runtime PR 应：

1. 写明它保证的架构不变量。
2. 只验证当前层次的责任：
   - Data Invariant：模型、约束、迁移和历史数据。
   - Engine Behavior：claim、lease、heartbeat、recovery 和 fencing。
   - Production Runtime：重启、断网、凭证失效和远端部分成功。
3. 提供与该层次匹配的 invariant tests。
4. 不修改未在 Issue 和 Design PR 中授权的核心行为。

README 应反映已经发布的能力。路线和设计变化先记录在 Milestone、
Issue、Design Spec 或 ADR 中，在 Release 准备阶段统一更新 README。

## Commit

建议使用 Conventional Commits，例如：

```text
feat(provider): add Aliyun Drive folder listing
fix(scan): avoid duplicate transfer tasks
docs: update Docker deployment guide
```
