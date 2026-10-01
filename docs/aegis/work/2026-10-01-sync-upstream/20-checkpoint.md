# 合并 LangBot 上游 master - Checkpoint

- Task ID: 2026-10-01-sync-upstream
- Current todo: 解决 21 个冲突文件
- Active slice: initial
- Blocked on: none
- Next step: 测试、构建、文档检查、提交合并

## Checkpoint Update

- Current todo: 完成历史资源迁移及 Linux 验证
- Active slice: 迁移闭环与跨平台验证
- Completed todos:
- 21 个冲突解决、前端构建及 189 单测、59 兼容回归、18 SQLite 迁移通过
- Evidence refs:
- docs/UPSTREAM_SYNC.md
- Blocked on: none
- Next step: 推送合并分支并执行 Linux CI

## DriftCheckDraft

- Scope status: 仍为上游合并
- Compatibility status: 保留用户定制；历史 RAG 修复限定旧 schema
- Retirement status: 旧会话列表实现已由上游索引取代，保留手动清理入口
- New risk signals:
- none
- Advisory decision: needs-verification

## Checkpoint Update

- Current todo: 复验最终合并分支
- Active slice: Linux CI 收尾
- Completed todos:
- Linux全部Python矩阵、快速集成、Box、启动及前端检查通过；修复PostgreSQL数字ID更新回归
- Evidence refs:
- evidence-bundle-draft-linux-ci-initial.json
- Blocked on: none
- Next step: 推送修复，等待最终CI并合并PR4到master
