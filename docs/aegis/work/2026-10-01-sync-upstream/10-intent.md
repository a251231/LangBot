# 合并 LangBot 上游 master - Intent

## TaskIntentDraft

- Requested outcome: 将 upstream/master 41820cd1 合并到用户仓库 master 并保留本地定制
- Goal: 将 upstream/master 41820cd1 合并到用户仓库 master 并保留本地定制
- Success evidence:
- none
- Stop condition: Stop when success evidence is satisfied or a blocker/risk requires pause.
- Non-goals:
- 当前主工作区未提交插件改动
- Scope: 核心、前端、迁移、发布流程的冲突整合及验证
- Change kinds:
- merge
- Risk hints:
- none

## BaselineReadSetHint

- AGENTS.md
- ARCHITECTURE.md
- .github/workflows/run-tests.yml

## ImpactStatementDraft

- Compatibility boundary: 保留飞书撤回、SQLite 重试、配置保存校验、微信去重及插件发布
- Affected layers:
- none
- Owners:
- none
- Invariants:
- none
- Non-goals:
- 当前主工作区未提交插件改动

These records are Method Pack drafts / hints, not authoritative runtime decisions.
