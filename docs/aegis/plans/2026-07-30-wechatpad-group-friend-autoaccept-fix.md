# WeChatPad 群聊好友申请自动同意修复计划

## Goal

修复 WeChatPadPro v861 中来自群聊的好友申请未实际通过、但 LangBot 误记成功的问题，并保持非群聊来源的现有行为兼容。

## Architecture

好友申请 XML 仍由 `wechatpad_friend_request.py` 解析；该模块把 `scene`、`V3`、`V4` 和可选的 `chatroomusername` 交给 `WeChatPadClient`，再由 `FriendApi` 映射为 v861 `VerifyUserRequestModel`。不新增第二个解析器或兼容入口。

## Tech Stack

- Python 3.11+
- `pytest`
- `ruff`
- WeChatPadPro v861 `/friend/AgreeAdd`
- Docker Compose 增量镜像部署

## Baseline/Authority Refs

- `AGENTS.md`
- `docs/aegis/BASELINE-GOVERNANCE.md`
- 生产事件：`msg_type=37`、`scene=14`、非空 `chatroomusername`
- WeChatPadPro v861 `/docs/swagger.json` 中的 `VerifyUserRequestModel`
- 基线提交 `0177333a9bddfca1a42bb6f7889b3b3d92fc4e37`

## Compatibility Boundary

- 群聊来源把 XML 的 `chatroomusername` 原样映射到 `ChatRoomUserName`。
- 非群聊来源缺少该字段时仍发送空字符串。
- `scene`、`V3`、`V4`、`OpCode=3` 和 `VerifyContent=''` 保持原样。
- 外层 `Code=200` 继续兼容；响应存在 `Data.BaseResponse.Ret` 时，只有 `Ret=0` 视为业务成功。
- 只重建 `langbot`，其他 Docker 容器 ID 必须保持不变。

## Verification

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH='D:\code\LangBot-wt-wechatpad-friend-group-fix\src'
D:\code\LangBot\.venv\Scripts\python.exe -m pytest tests\unit_tests\platform\test_wechatpad.py -q
D:\code\LangBot\.venv\Scripts\python.exe -m ruff check `
  src\langbot\libs\wechatpad_api\api\friend.py `
  src\langbot\libs\wechatpad_api\client.py `
  src\langbot\pkg\platform\sources\wechatpad_friend_request.py `
  tests\unit_tests\platform\test_wechatpad.py
```

## Scope Check

- Fact：生产已调用 `/friend/AgreeAdd`，原始事件是 `scene=14` 且群 ID 非空。
- Fact：当前 `FriendApi` 固定发送 `ChatRoomUserName=''`。
- Fact：v861 Swagger 明确要求通过群添加时传群 ID。
- Assumption：v861 成功响应沿用外层 `Code=200`；内层 `Ret` 存在时是更强的业务状态。
- Unknown：原失败请求的完整响应体没有被历史日志保留；部署后通过实际重放当前待处理申请验证。

### Ripple Signal Triage

- Canonical owner：`wechatpad_friend_request.py` 拥有 XML 解析，`FriendApi` 拥有 API 字段映射。
- Producer/consumer：事件解析、客户端委托、HTTP 请求三层签名同时更新并分别测试。
- Contract：新增可选 `chatroom_username: str = ''`，保持现有调用者兼容。
- Verification：单元测试覆盖群聊、非群聊、内层业务失败；生产验证请求、响应和容器隔离。

## Task 1: 用失败测试固定跨层合同

**Files:** modify `tests/unit_tests/platform/test_wechatpad.py`

**Why:** 复现生产 `scene=14` 样本，并证明失败来自群 ID 丢失和过宽成功判断。

**Impact/Compatibility:** 仅测试；现有 16 项测试保持通过，新测试在生产代码修改前失败。

**Verification:** 运行目标测试文件，预期新增断言因缺少 `chatroom_username` 参数或错误成功日志而失败。

- [x] 新增 `FriendApi` 测试，期望 `ChatRoomUserName='room@chatroom'`。
- [x] 新增客户端委托测试，期望可选群 ID 原样下传。
- [x] 把群聊事件测试改为携带 `chatroomusername`，期望处理器透传。
- [x] 新增 `Data.BaseResponse.Ret != 0` 测试，期望记录失败而不是成功。
- [x] 运行测试并记录预期 RED，不修改生产代码。

## Task 2: 最小实现并完成回归

**Files:** modify `src/langbot/libs/wechatpad_api/api/friend.py`, `src/langbot/libs/wechatpad_api/client.py`, `src/langbot/pkg/platform/sources/wechatpad_friend_request.py`, `tests/unit_tests/platform/test_wechatpad.py`

**Why:** 在现有唯一调用链上恢复 v861 群聊好友验证合同。

**Impact/Compatibility:** 新参数有默认空值；现有非群聊调用保持原请求形态。

**Verification:** 运行计划顶部完整测试和 Ruff 命令，预期退出码均为 `0`。

- [x] 在处理器解析 `chatroomusername` 并传给客户端。
- [x] 在客户端和 `FriendApi` 增加默认空值参数并映射到 `ChatRoomUserName`。
- [x] 在处理器仅在响应显式带内层 `Ret` 时校验其为 `0`，失败日志包含状态码而不包含敏感响应体。
- [x] 运行全量 WeChatPad 测试和 Ruff，审查差异只覆盖上述合同。
- [x] 提交 `fix(wechatpad): pass group context for friend approval`，推送修复分支。

## Deployment

- 基于当前生产镜像构建只覆盖相关 Python 文件的增量镜像。
- 备份服务器对应文件和 Compose 覆盖文件。
- 仅执行 `docker compose ... up -d --no-deps --force-recreate langbot`。
- 核对其他容器 ID、HTTP 200、启动日志、镜像内文件哈希和机器人开关。
- 使用 Redis 中保留的当前 `scene=14` 申请数据调用修复后的接口，验证业务响应；不输出申请人或令牌信息。

## Risks

- 当前申请可能已过期；此时未来申请链路仍可通过新事件验证，当前重放会返回可诊断业务状态。
- WeChatPadPro 响应结构缺少正式 schema，因此只对实际存在的内层 `Ret` 加强判断，不猜测其他字段。

## Repair Track

- Root cause：群聊来源 `chatroomusername` 在事件解析到 API 请求之间丢失。
- Minimal change：沿现有三层调用链增加一个可选字符串参数。
- Verification：生产样本单测、完整 WeChatPad 回归、实际 v861 请求。

## Retirement Track

- Retired logic：`FriendApi` 固定 `ChatRoomUserName=''`。
- Retained boundary：缺少群 ID的非群聊来源仍使用空字符串。
- Retirement trigger：本次提交立即删除固定空值 owner，不保留 fallback。
