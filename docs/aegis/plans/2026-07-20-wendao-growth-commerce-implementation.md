# 问道签到助手社区增长与积分商城实施计划

## Goal

在 `wendao_signin_plugin` 中实现已批准的一层推广、双向积分、时长卡密商城和使用权益控制，同时保持既有账号凭据、签到、补签、周报和调度数据兼容。

## Architecture

`ArchitectureReviewRequired: yes`

- `AccountStore` 继续拥有 `AccountRecord v1`、凭据和签到调度状态。
- 新增 `GrowthStore` 作为增长域持久化入口，按 `bot_uuid` 和身份摘要分片。
- `GrowthService` 是增长业务编排入口，内部组合积分、推广和商城服务。
- `EntitlementService` 负责试用期、到期判断和卡密延期。
- 多记录操作先写 `PENDING` 操作日志，各步骤按 `operation_id` 幂等应用，最后写 `COMMITTED`。
- 单个机器人使用独立 `asyncio.Lock` 串行化积分、库存、卡密和权益写操作。

## Tech Stack

- Python 3.11+
- `langbot-plugin==0.3.8`
- LangBot BinaryStorage API
- `dataclasses`、`asyncio`、`hashlib`、`json`、`secrets`
- `cryptography`，复用插件已有依赖
- `pytest`、`ruff`、`mypy`

## Baseline/Authority Refs

- `AGENTS.md`
- `docs/aegis/specs/2026-07-20-wendao-growth-commerce-design.md`
- `docs/aegis/BASELINE-GOVERNANCE.md`
- `plugins/wendao_signin_plugin/manifest.yaml`
- `plugins/wendao_signin_plugin/main.py`
- `plugins/wendao_signin_plugin/components/account_store.py`
- `plugins/wendao_signin_plugin/components/workflow.py`
- `plugins/wendao_signin_plugin/components/scheduler.py`
- `plugins/wendao_signin_plugin/components/command_parser.py`
- `plugins/wendao_signin_plugin/components/event_listeners/wendao_signin_listener.py`
- `plugins/wendao_signin_plugin/tests/`

## Compatibility Boundary

- `AccountRecord` 字段和 `account:v1:` 序列化格式保持原样。
- 既有问道命令解析结果和回复语义保持原样；新增命令只使用新的精确前缀。
- `问道解绑` 删除账号凭据，保留增长域关系、积分、兑换记录和权益。
- 到期控制只包围问道查询、签到、补签、周报和自动调度。
- 管理命令、推广、积分、商城、兑换、激活和权益查询始终保留入口。
- 组件 YAML 和插件加载入口保持现有路径。
- 实现阶段保留 manifest 版本 `0.4.3`；发布阶段经用户确认后单独升级到 `0.5.0`。

## Verification

```powershell
$env:PYTHONPATH='D:\code\LangBot\plugins\wendao_signin_plugin'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests -q
D:\code\LangBot\.venv\Scripts\python.exe -m ruff check plugins\wendao_signin_plugin
D:\code\LangBot\.venv\Scripts\python.exe -m mypy `
  plugins\wendao_signin_plugin\components\growth_models.py `
  plugins\wendao_signin_plugin\components\growth_store.py `
  plugins\wendao_signin_plugin\components\points.py `
  plugins\wendao_signin_plugin\components\referral.py `
  plugins\wendao_signin_plugin\components\commerce.py `
  plugins\wendao_signin_plugin\components\entitlement.py `
  plugins\wendao_signin_plugin\components\growth_service.py `
  --ignore-missing-imports
```

## Scope Check

### Facts

- 插件当前拥有 181 个通过的单元测试。
- 插件存储只提供按键读写、删除和列出键，没有跨键数据库事务。
- `SigninWorkflow` 的最终签到状态来自 `list_signin()`。
- 账号身份由 `(bot_uuid, sender_id)` 定位，问道身份优先使用 `user_identifier`。
- 当前插件 Runtime 按独立插件进程运行，适合进程内异步锁。
- `plugins/wendao_signin_plugin/` 当前整目录处于 Git 未跟踪状态，功能开发前需要先建立可复现源码基线。

### Assumptions

- 一个增长数据分片同时只有一个插件进程写入。
- BinaryStorage 单键写入具有原子替换语义。
- 单机器人约 1 万用户和数万张卡密属于目标容量。

### Unknowns To Measure

- 目标数据库在 5 万增长键下的 `get_plugin_storage_keys()` 延迟。
- 1,000 张卡密单批生成和持久化的峰值耗时。
- 启动恢复大量 `PENDING` 操作时的耗时。

### Ripple Signal Triage

- Owner 扩展：新增增长域和权益域 owner。
- 下游扩展：命令解析、插件主类、绑定流程、签到工作流、调度器、README、manifest 和包契约测试。
- 合同扩展：新增用户命令、管理员命令、manifest 配置和 `growth:*:v1` 存储键。
- 事实源扩展：积分流水和卡密状态成为新事实源，余额和库存是快照。
- 验证扩展：覆盖幂等、并发、中断恢复、敏感字段、旧命令回归和插件重启。

## File Map

### Create

- `docs/aegis/adr/0001-wendao-growth-store.md`
- `plugins/wendao_signin_plugin/components/growth_models.py`
- `plugins/wendao_signin_plugin/components/growth_store.py`
- `plugins/wendao_signin_plugin/components/points.py`
- `plugins/wendao_signin_plugin/components/referral.py`
- `plugins/wendao_signin_plugin/components/commerce.py`
- `plugins/wendao_signin_plugin/components/entitlement.py`
- `plugins/wendao_signin_plugin/components/growth_service.py`
- `plugins/wendao_signin_plugin/tests/test_growth_store.py`
- `plugins/wendao_signin_plugin/tests/test_points.py`
- `plugins/wendao_signin_plugin/tests/test_referral.py`
- `plugins/wendao_signin_plugin/tests/test_commerce.py`
- `plugins/wendao_signin_plugin/tests/test_entitlement.py`
- `plugins/wendao_signin_plugin/tests/test_growth_integration.py`

### Modify

- `plugins/wendao_signin_plugin/manifest.yaml`
- `plugins/wendao_signin_plugin/main.py`
- `plugins/wendao_signin_plugin/README.md`
- `plugins/wendao_signin_plugin/components/command_parser.py`
- `plugins/wendao_signin_plugin/components/workflow.py`
- `plugins/wendao_signin_plugin/components/scheduler.py`
- `plugins/wendao_signin_plugin/tests/test_command_parser.py`
- `plugins/wendao_signin_plugin/tests/test_event_listener.py`
- `plugins/wendao_signin_plugin/tests/test_plugin.py`
- `plugins/wendao_signin_plugin/tests/test_workflow.py`
- `plugins/wendao_signin_plugin/tests/test_scheduler.py`
- `plugins/wendao_signin_plugin/tests/test_package_contract.py`

## Tasks

### Task 1: 记录增长域架构决策

**Files:** create `docs/aegis/adr/0001-wendao-growth-store.md`, modify `docs/aegis/INDEX.md`

**Why:** 固定 `GrowthStore`、操作日志、单进程锁和数据库升级触发条件，防止实施时出现多个事实源。

**Impact/Compatibility:** 文档变更；运行时代码保持原状。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe C:\Users\test0023\.codex\aegis\scripts\aegis-workspace.py check --root D:\code\LangBot`

- [ ] 写 ADR：状态设为 `Accepted`，记录候选方案“扩展 AccountRecord”“单 JSON 文档”“分片记录加操作日志”，结论采用第三种。
- [ ] 记录 owner：`AccountStore` 拥有账号状态，`GrowthStore` 拥有增长状态，积分流水与卡密状态是事实源。
- [ ] 记录升级触发条件：多进程共享、单机器人超过 1 万用户或恢复耗时超过 30 秒时评估外部数据库。
- [ ] 使用 `append-index --kind adr --title "问道增长域存储与一致性"` 更新索引并运行 workspace check，预期退出码 `0`。
- [ ] 只暂存 ADR 和索引：`git add -- docs/aegis/adr/0001-wendao-growth-store.md docs/aegis/INDEX.md`；确认 `git diff --cached --name-only` 只包含这两个路径后提交：`git commit -m "docs(aegis): record wendao growth storage decision"`。

### Task 2: 建立当前插件源码基线

**Files:** add current `plugins/wendao_signin_plugin/` source, tests, README, manifest, requirements and icon; exclude `dist/` and caches

**Why:** 后续功能提交必须能够从任意提交独立检出、运行测试和审查。

**Impact/Compatibility:** 只纳入当前 `0.4.3` 源码；运行行为保持当前基线。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests -q` and `D:\code\LangBot\.venv\Scripts\python.exe -m ruff check plugins\wendao_signin_plugin`

- [ ] 运行 `git status --short -- plugins/wendao_signin_plugin`，确认目录为未跟踪状态，并记录当前 `0.4.3` 包 SHA-256 `F7661304C0EF6CE30ADF37AEEE5BE3CFA2CB891DBEDF2EF8CBFE0A67C4C0710D`。
- [ ] 运行当前全量插件测试，预期 `181 passed`；运行 Ruff，预期 `All checks passed!`。
- [ ] 精确暂存 `README.md`、`manifest.yaml`、`requirements.txt`、`main.py`、`assets/`、`components/` 和 `tests/`，保持 `dist/`、`__pycache__/` 与测试缓存在暂存区外。
- [ ] 运行 `git diff --cached --name-only`，确认每条路径都位于 `plugins/wendao_signin_plugin/` 且没有 `/dist/` 或缓存路径。
- [ ] 提交：`git commit -m "chore(wendao): add plugin source baseline"`；确认提交成功后运行 `git worktree add -b dev/wendao-growth-commerce D:\code\LangBot-wt-wendao-growth-commerce HEAD`，再在该工作树执行 Task 3 至 Task 11；若目标目录已存在，先运行 `git worktree list` 核对其是否正是该分支，不删除或覆盖现有目录。

### Task 3: 固定配置和命令解析合同

**Files:** modify `manifest.yaml`, `components/command_parser.py`, `tests/test_command_parser.py`, `tests/test_package_contract.py`

**Why:** 先锁定所有公开命令和配置键，后续服务实现以稳定合同为输入。

**Impact/Compatibility:** 原命令仍返回原 `ParsedCommand`；新命令使用独立 kind。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_command_parser.py plugins\wendao_signin_plugin\tests\test_package_contract.py -q`

- [ ] 写失败测试：参数化断言 `问道推广→promotion`、`问道邀请 ABCD2345→referral_bind`、`问道积分→points`、`问道商城→shop`、`问道兑换 P000001→redeem`、`问道兑换记录→redemptions`、`问道激活 WD-TEST→activate`、`问道权益→entitlement`、`问道管理 商品列表→admin`。
- [ ] 运行目标测试，预期新命令断言失败且原命令断言继续通过。
- [ ] 最小实现：在最长前缀优先表中加入 `问道兑换记录` 和 `问道管理`，在精确命令表中加入无参数命令；新增 manifest 配置 `admin_user_ids`、`growth_trial_days=30`、`promoter_reward_points=100`、`invitee_reward_points=20`。
- [ ] 更新包契约测试，精确断言四个配置默认值和 README 待新增命令；运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/manifest.yaml plugins/wendao_signin_plugin/components/command_parser.py plugins/wendao_signin_plugin/tests/test_command_parser.py plugins/wendao_signin_plugin/tests/test_package_contract.py; git commit -m "feat(wendao): define growth command contracts"`。

### Task 4: 建立增长模型、键空间和分片存储

**Files:** create `components/growth_models.py`, `components/growth_store.py`, `tests/test_growth_store.py`

**Why:** 为推广、积分、商品、卡密、权益和操作日志建立独立、可版本化的持久化基础。

**Impact/Compatibility:** 使用新增键前缀；`account:v1:` 数据保持原样。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_growth_store.py plugins\wendao_signin_plugin\tests\test_account_store.py -q`

- [ ] 写失败测试：覆盖身份键中不出现原始 ID、不同 bot 隔离、dataclass JSON 往返、500 条分片边界、损坏记录跳过并返回诊断计数。
- [ ] 运行目标测试，预期导入 `components.growth_store` 失败。
- [ ] 最小模型：实现 `PromoterRecord`、`ReferralRecord`、`PointAccount`、`PointEntry`、`ProductRecord`、`CardRecord`、`RedemptionRecord`、`EntitlementRecord`、`GrowthOperation`，全部使用 frozen/slots dataclass 和显式 `schema_version=1`。
- [ ] 最小存储：实现 `GrowthStore.get/save/delete/list_prefix`、`identity_hash(bot_uuid, sender_id)`、`bot_lock(bot_uuid)`、固定 500 条的 `append_sharded_index` 和 `read_sharded_index`；运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/components/growth_models.py plugins/wendao_signin_plugin/components/growth_store.py plugins/wendao_signin_plugin/tests/test_growth_store.py; git commit -m "feat(wendao): add growth storage foundation"`。

### Task 5: 实现操作日志和积分账本

**Files:** modify `components/growth_models.py`, `components/growth_store.py`; create `components/points.py`, `tests/test_points.py`

**Why:** 所有奖励和兑换依赖可审计、可恢复、幂等的积分变更。

**Impact/Compatibility:** 新增长域内部合同；普通签到路径尚未接入。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_growth_store.py plugins\wendao_signin_plugin\tests\test_points.py -q`

- [ ] 写失败测试：同一 `operation_id` 重放只产生一条流水；双用户奖励两边各加一次；余额不足扣减保持零变更；中断后从 `PENDING.applied_steps` 继续。
- [ ] 运行目标测试，预期 `PointService` 导入或行为断言失败。
- [ ] 最小实现：`PointService.balance()`、`credit()`、`debit()`、`adjust()`；流水键使用 `point-entry:v1:<bot>:<entry_id>`，账户快照保存 `balance` 和 `last_entry_id`。
- [ ] 在 `GrowthStore` 实现 `begin_operation()`、`mark_step_applied()`、`commit_operation()`、`list_pending_operations()`；每一步写入后再推进 `applied_steps`，运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/components/growth_models.py plugins/wendao_signin_plugin/components/growth_store.py plugins/wendao_signin_plugin/components/points.py plugins/wendao_signin_plugin/tests/test_growth_store.py plugins/wendao_signin_plugin/tests/test_points.py; git commit -m "feat(wendao): add recoverable point ledger"`。

### Task 6: 实现推广关系和双向奖励

**Files:** create `components/referral.py`, `tests/test_referral.py`; modify `components/growth_models.py`, `components/points.py`

**Why:** 将邀请码登记、绑定、有效确认和奖励发放封装为唯一推广 owner。

**Impact/Compatibility:** 只新增服务；绑定和签到入口在后续任务接入。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_referral.py plugins\wendao_signin_plugin\tests\test_points.py -q`

- [ ] 写失败测试：稳定 8 位邀请码、碰撞重试、首码锁定、已绑定用户拒绝登记、自邀拒绝、`pending→bound→effective`、同一 `user_identifier` 只计一次、双向奖励重放幂等、汇总口径和 7 天窗口。
- [ ] 运行目标测试，预期 `ReferralService` 导入或状态断言失败。
- [ ] 最小实现：`ensure_promoter()`、`register_invite()`、`on_account_bound()`、`on_signin_confirmed()`、`stats()`；邀请码字符集固定为 `23456789ABCDEFGHJKLMNPQRSTUVWXYZ`。
- [ ] 有效奖励使用 `referral-effective:<referral_id>` 作为操作 ID，并把当时的推广员/受邀人积分写入操作 payload；运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/components/growth_models.py plugins/wendao_signin_plugin/components/points.py plugins/wendao_signin_plugin/components/referral.py plugins/wendao_signin_plugin/tests/test_referral.py; git commit -m "feat(wendao): add referral attribution and rewards"`。

### Task 7: 实现试用期和权益检查

**Files:** create `components/entitlement.py`, `tests/test_entitlement.py`; modify `components/growth_models.py`, `components/growth_store.py`

**Why:** 统一处理存量试用、新用户试用、到期判断和卡密延期公式。

**Impact/Compatibility:** 先建立服务合同，业务命令和调度门控在后续任务接入。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_entitlement.py -q`

- [ ] 写失败测试：首次初始化固定 `rollout_at`、存量账号到期时间一致、新绑定按绑定时间计算、重复绑定不重置、过期判定、过期后延期从当前时间计算、未过期延期从原到期时间计算。
- [ ] 运行目标测试，预期 `EntitlementService` 导入或行为断言失败。
- [ ] 最小实现：`initialize_existing_accounts()`、`ensure_for_binding()`、`get_status()`、`require_active()`、`extend()`；统一抛出 `EntitlementExpiredError` 和 `EntitlementStorageError`。
- [ ] 初始化使用 `growth-rollout:<bot_uuid>` 操作 ID，账号逐项记录 applied step，支持中断恢复；运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/components/growth_models.py plugins/wendao_signin_plugin/components/growth_store.py plugins/wendao_signin_plugin/components/entitlement.py plugins/wendao_signin_plugin/tests/test_entitlement.py; git commit -m "feat(wendao): add usage entitlement service"`。

### Task 8: 实现商品、卡密库存、兑换和激活

**Files:** create `components/commerce.py`, `tests/test_commerce.py`; modify `components/growth_models.py`, `components/growth_store.py`, `components/points.py`, `components/entitlement.py`

**Why:** 完成积分到可转赠时长卡密的闭环，并保证扣分、发卡和激活可恢复。

**Impact/Compatibility:** 新增长域服务；复用已有 `cryptography` 依赖。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_commerce.py plugins\wendao_signin_plugin\tests\test_points.py plugins\wendao_signin_plugin\tests\test_entitlement.py -q`

- [ ] 写失败测试：管理员创建/上下架商品、输入上下限、批量生成库存、卡密原文加密且键只含哈希、余额不足/下架/零库存零变更、兑换成功可重复查询、跨用户激活、二次激活幂等、商品变更不影响卡密快照。
- [ ] 运行目标测试，预期 `CommerceService` 导入或行为断言失败。
- [ ] 最小实现：`create_product()`、`set_enabled()`、`add_inventory(..., request_id: str)`、`list_products()`、`redeem(..., request_id: str)`、`list_redemptions()`、`activate()`；卡密格式固定为 `WD-` 加 32 字节 URL-safe 随机值，管理员库存操作和用户兑换都要求非空请求 ID。
- [ ] 兑换操作 ID 使用 `redeem:<bot>:<sender>:<request_id>`，激活操作 ID 使用 `activate:<card_hash>`；事务步骤记录扣分、卡密 `ISSUED`、兑换索引、卡密 `ACTIVATED` 和权益延期，运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/components/growth_models.py plugins/wendao_signin_plugin/components/growth_store.py plugins/wendao_signin_plugin/components/points.py plugins/wendao_signin_plugin/components/entitlement.py plugins/wendao_signin_plugin/components/commerce.py plugins/wendao_signin_plugin/tests/test_commerce.py; git commit -m "feat(wendao): add point redemption and cards"`。

### Task 9: 建立 GrowthService 并接入用户与管理员命令

**Files:** create `components/growth_service.py`, `tests/test_growth_integration.py`; modify `main.py`, `tests/test_plugin.py`

**Why:** 让主插件只负责配置、权限和命令路由，增长业务保持单一入口。

**Impact/Compatibility:** `WendaoSigninPlugin.initialize()` 新增增长服务初始化；原服务实例和命令分支保持。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_growth_integration.py plugins\wendao_signin_plugin\tests\test_plugin.py -q`

- [ ] 写失败测试：初始化创建 GrowthStore/GrowthService/EntitlementService；非管理员管理命令被拒绝；所有用户命令回复含预期汇总且不泄露卡密；解绑后积分和权益保留；配置字符串去重和空白处理。
- [ ] 运行目标测试，预期插件缺少增长服务或命令落入未知分支。
- [ ] 最小实现：GrowthService 暴露 `promotion()`、`bind_referral()`、`points()`、`shop()`、`redeem(..., request_id: str)`、`redemptions()`、`activate()`、`entitlement()`、`admin(..., request_id: str)`、`on_account_bound()`、`on_signin_confirmed()`、`recover_pending()`；所有会改变积分或库存的入口要求非空请求 ID。
- [ ] 在 `main.py` 初始化服务、执行存量账号 rollout、按 `admin_user_ids` 校验管理命令，并在 `_verify_and_save_binding` 保存后调用 `on_account_bound()`；运行目标测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/main.py plugins/wendao_signin_plugin/components/growth_service.py plugins/wendao_signin_plugin/tests/test_plugin.py plugins/wendao_signin_plugin/tests/test_growth_integration.py; git commit -m "feat(wendao): expose growth and admin commands"`。

### Task 10: 接入签到确认和调度权益门控

**Files:** modify `components/workflow.py`, `components/scheduler.py`, `components/event_listeners/wendao_signin_listener.py`, `main.py`, `tests/test_workflow.py`, `tests/test_scheduler.py`, `tests/test_event_listener.py`, `tests/test_growth_integration.py`

**Why:** 让首次签到成为有效拉新事实，并让过期账号同时停止手动与自动问道业务。

**Impact/Compatibility:** `SigninWorkflow` 和 `SigninScheduler` 新增可选依赖，默认值保持现有测试与第三方构造方式兼容。

**Verification:** `D:\code\LangBot\.venv\Scripts\python.exe -m pytest plugins\wendao_signin_plugin\tests\test_workflow.py plugins\wendao_signin_plugin\tests\test_scheduler.py plugins\wendao_signin_plugin\tests\test_event_listener.py plugins\wendao_signin_plugin\tests\test_growth_integration.py -q`

- [ ] 写失败测试：最终 `signInStatus==2` 调用一次确认回调；回调异常不改变签到结果；过期账号调度跳过每日和周报；权益读取异常跳过业务并产生可诊断结果；推广/商城命令在过期状态继续执行；监听器把 `query_id` 作为 `request_id` 传入插件。
- [ ] 运行目标测试，预期新构造参数或门控断言失败。
- [ ] 最小实现：`SigninWorkflow.__init__` 增加可选 `on_signin_confirmed: Callable[[AccountRecord, datetime], Awaitable[None]]`；`SigninScheduler.__init__` 增加可选 `is_entitled: Callable[[AccountRecord], Awaitable[bool]]`。
- [ ] `main.py` 在手动 query/signin/resign/weekly_report 前调用 `require_active()`，向 workflow/scheduler 注入增长回调，并给 `handle_wendao_command` 增加默认值为空的 `request_id`；监听器传入字符串化 `query_id`，运行目标测试和全部旧 workflow/scheduler 测试，预期全部通过。
- [ ] 提交：`git add -- plugins/wendao_signin_plugin/main.py plugins/wendao_signin_plugin/components/workflow.py plugins/wendao_signin_plugin/components/scheduler.py plugins/wendao_signin_plugin/components/event_listeners/wendao_signin_listener.py plugins/wendao_signin_plugin/tests/test_workflow.py plugins/wendao_signin_plugin/tests/test_scheduler.py plugins/wendao_signin_plugin/tests/test_event_listener.py plugins/wendao_signin_plugin/tests/test_growth_integration.py; git commit -m "feat(wendao): gate workflows by entitlement"`。

### Task 11: 文档、全量回归和发布准备

**Files:** modify `README.md`, `tests/test_package_contract.py`; review all growth files and manifest

**Why:** 确保用户、管理员、部署者和发布者掌握命令、配置、数据安全与容量边界。

**Impact/Compatibility:** 文档和测试；manifest 版本保持 `0.4.3`，发布动作另行确认。

**Verification:** execute the full `Verification` command block at the top of this plan, then run `D:\code\LangBot\.venv\Scripts\lbp.exe build -o $env:TEMP\wendao-lbp-plan-verify` from `plugins\wendao_signin_plugin`

- [ ] 更新 README：加入推广、积分、商城、管理员命令、试用/到期规则、卡密敏感性、单进程与 1 万用户容量边界，并保留全部旧登录和签到文档。
- [ ] 更新包契约测试：断言新命令、四个配置、新模块文件和安全说明；运行该测试，预期通过。
- [ ] 运行全量测试和 Ruff，预期全部通过；记录测试数量、警告来源和执行耗时。
- [ ] 运行 scoped mypy；随后执行 `lbp build -o $env:TEMP\wendao-lbp-plan-verify`，检查包内 manifest、组件 YAML、新模块和版本一致，并清理 `$env:TEMP\wendao-lbp-plan-verify`。
- [ ] 提交：`git add plugins/wendao_signin_plugin && git commit -m "docs(wendao): document growth and card workflows"`；版本升级和正式 `.lbpkg` 进入单独发布步骤。

## Risks

- BinaryStorage 缺少跨键事务，依赖操作日志实现最终一致性；每个步骤必须幂等。
- 单进程锁是明确边界，多实例同时写同一 bot 数据时存在竞态。
- 增长域密钥丢失会影响未激活卡密恢复，必须随插件存储备份。
- 大量存储键会放大 `get_plugin_storage_keys()` 成本，计划包含 5 万键基准测试。
- 兑换回复投递失败时用户依赖兑换记录取回卡密，兑换记录必须先于回复持久化。
- 管理员配置错误会导致管理入口缺失，README 必须提供查询用户 ID 的明确方法。

## Retirement

- 旧 owner：不存在推广、积分、商城或权益 owner，本计划不迁移旧增长数据。
- 保留路径：`AccountStore`、`SigninWorkflow` 和 `SigninScheduler` 保留原职责，通过可选回调扩展。
- 兼容载体：可选回调只服务现有构造兼容，正式插件始终注入增长服务。
- 退出触发：多进程共享、单 bot 超过 1 万用户、5 万键扫描不可接受或恢复超过 30 秒时，制定外部数据库迁移 ADR。
- 延后能力：实体商品、人工订单、外部发卡、多级返佣、支付和密钥轮换。

## Execution Order

严格按 Task 1 到 Task 11 执行。Task 1 和 Task 2 在当前工作区完成；Task 2 建立源码基线后创建 `dev/wendao-growth-commerce` 独立工作树。Task 4 是 Task 5 至 Task 8 的基础；Task 9 只在领域服务单测通过后接入主插件；Task 10 最后改变既有签到和调度行为；Task 11 负责全量闭环。
