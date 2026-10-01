# 上游合并执行计划

- 目标：合并 upstream/master 41820cd1 到 origin/master 5d1f2392，保留用户已有定制。
- 架构：采用上游 Agent、工作区与持久化架构；定制行为留在原有责任模块。
- 技术栈：Python / uv，React / Vite / pnpm，SQLite / PostgreSQL / Alembic。
- 基线：AGENTS.md、ARCHITECTURE.md、CI workflow、双方 Git 提交。
- 兼容边界：飞书主动消息和撤回桥接、SQLite 锁重试、插件配置保存检查、微信去重、中文字体、Fork 镜像发布。
- 验证：后端单元与 smoke、快速集成与迁移、前端单元、构建、lint、Git 祖先关系和冲突标记检查。

1. 复用干净同步工作区，从 origin/master 创建 dev/sync-upstream-20261001。
2. 获取上游 master，执行保留历史的 merge，逐文件解决 21 个冲突。
3. 对锁重试和会话清理兼容补充回归测试，先验证失败再整合实现。
4. 检查自动合并的工作区作用域、配置读回和旧迁移图，运行对应回归。
5. 更新当前文档及证据记录，提交合并，更新用户仓库 master。

采用上游会话索引和租户事务设计，退休旧版列表缓存及无作用域访问；只对既有配置入口保留兼容方法。回退通过合并前提交 5d1f2392 和合并分支保留。
