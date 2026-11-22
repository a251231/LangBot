# 上游同步与 Fork 兼容规则

本仓库以 `langbot-app/LangBot` 的 `master` 为上游。2026-11-22 同步基点为 `9603b543`（上游版本 `4.11.9` 及后续 `master` 提交），Plugin SDK 已随上游并入仓库 `sdk/` 目录，版本为 `0.7.26`。运行链路和权限边界以 `ARCHITECTURE.md` 为准。

## 当前兼容行为

- **WeChatFerry 适配器**：保留 [wechatferry.py](../src/langbot/pkg/platform/sources/wechatferry.py) 及其配置模板与单测（含 Helper V4-V7 内联 Base64 图片、发送者/群名解析、64MiB WS 帧与异步分发）。
- **WeChatPad 增强**：保留消息去重、群提及解析、同步信封 `AddMsgs` 逐条提交到主事件循环、部署文本筛选规则以及默认关闭的 `auto_accept_friend` 好友申请处理与单测。
- **纯插件部署模式 (`system.plugin_only`)**：保留未认领消息静默、跳过 Agent 路由以及无模型/纯插件 Pipeline 的配置迁移支持（详见 [plugin-only-deployment.zh-CN.md](plugin-only-deployment.zh-CN.md)）。
- **飞书增强与监控显示名**：
  - 飞书主动消息保留目标类型映射与租户凭据，同时支持上游的 Markdown 表格消息与媒体发送。
  - 历史撤回事件 `MessageRecalled` 桥接继续通过当前消息事件绑定取得 pipeline，并传递完整工作区执行上下文。
  - 监控会话与消息采用上游 `SessionSummaryWriter` 与 `extract_sender_name` 统一写入 `user_name`；当消息无发送者名称时回退至群名，CSV 导出与前端类型保留 `user_name`。
- **核心稳定性加固**：
  - SQLite 连接配置（WAL、`busy_timeout=30000`、`journal_size_limit`、`auto_vacuum=INCREMENTAL`）与 `telemetry/platform.py` 的 `object.__setattr__` 修复已由上游原生吸收，直接采用上游实现。
  - `persistence/mgr.py` 独立查询继续保留有限次数的 SQLite 锁重试；工作区事务保持上游事务所有权与回滚规则。
  - 手动清理闲置会话继续保留在 `sessionmgr.py` 与 `system.py`，执行中会话不被清理。
  - 插件配置保存后按工作区读回校验，HTTP 响应返回脱敏配置，前端 `PluginForm` 据此确认保存并清理未使用附件；保留 `N8nAuthFormComponent` 与 `MCPForm` 表单校验修复。
- **插件目录回归上游 Monorepo**：原 Fork 内置的 3 个飞书自研插件（`feishu_ai_analysis_plugin`、`feishu_keyword_report_plugin`、`feishu_process_bitable_plugin`）及 `build-plugins.yml` 已拆分至独立仓库并从本仓库移除；`plugins/` 目录完全对齐上游官方插件 Monorepo。
- **Fork Docker、CI 与发布定制**：
  - Docker 镜像在采用上游多阶段构建的基础上保留中文字体（`fontconfig`、`fonts-noto-cjk`）。
  - Docker 镜像发布至 Fork 配置的 `${{ secrets.DOCKER_USERNAME }}/langbot` 仓库（包含上游新增的 `seekdb`/`milvus`/`valkey` extra 变体）；`docker/docker-compose.yaml` 中 `langbot`、`langbot_plugin_runtime`、`langbot_box` 均指向 `a251231/langbot:master` 并启用 Watchtower 跟踪。
  - 前端版本更新检查指向本 Fork 仓库 `a251231/LangBot` 的 GitHub Releases。
  - CLA 签名与 PyPI 发布 workflow 仅在 `langbot-app/LangBot` 上游仓库运行。

## 数据库升级

- `0057_merge_fork_upstream_heads` 汇合上游最新节点 `0056_daily_usage_quota_warning` 与 Fork 历史修复分支头 `0035_monitoring_tool_workspace`（其父节点为 `0034_merge_fork_rag_repair`），使 Alembic 保持单一 head，不额外修改业务表结构。
- 已有数据库中的历史长版本号通过 `alembic_runner.py` 的别名表归一化，已部署的旧 Fork 实例可直接无缝升级至最新 schema。
- 升级运行实例前请先备份数据库和配置，并验证独立仓库维护的自研插件与 SDK `0.7.26` 的实际运行兼容性。
