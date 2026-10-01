# 合并 LangBot 上游 master - Evidence

No evidence has been recorded yet.

## EvidenceBundleDraft

- Artifact key: local-validation
- Type: command
- Source: 本地 Windows Python 3.12、pnpm 9 的验证日志
- Summary: 前端构建及 189 单测通过；Ruff 检查及格式通过；59 项兼容回归、37 项迁移与配置安全回归通过。广泛回归 5042 单测通过，358 快速集成通过；Windows 环境差异及旧迁移重放已分别定位，Linux CI 待验证。
- Verifier: Codex

## EvidenceBundleDraft

- Artifact key: linux-ci-initial
- Type: command
- Source: GitHub Actions PR4 / 6cbb2f26
- Summary: Linux Python3.11/3.12/3.13 全量单测、快速集成、Box、启动、前端端到端、Lint、SQLite迁移通过。PostgreSQL64通过2失败，定位会话更新数字ID遗漏归一化；新本地回归先3失败再23通过1跳过。CLA上游专属凭据配置已加Fork guard，最后head复验待运行。
- Verifier: Codex

## EvidenceBundleDraft

- Artifact key: linux-ci-postgres
- Type: command
- Source: GitHub Actions 36871080957 / 25fcfd8b
- Summary: PostgreSQL迁移、监控RLS、向量库、发布迁移及插件身份场景66项全部通过；SQLite通过。GitHub Actions 36871081156 三组Python全量单测、快速集成、Box和启动全部通过。最终覆盖率和前端端到端待PR4检查，CLA旧base缺上游PAT与Fork功能无关。
- Verifier: Codex
