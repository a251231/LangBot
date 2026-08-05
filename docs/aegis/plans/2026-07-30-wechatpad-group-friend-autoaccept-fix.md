# WeChatPad 群聊好友申请自动同意修复计划

## Goal

修复 WeChatPadPro v861 中来自群聊的好友申请未实际通过、但 LangBot 误记成功的问题，并确保 WebSocket 同步包中的 `AddMsgs` 会逐条进入现有消息处理链，同时保持非群聊来源的现有行为兼容。

## Architecture

WebSocket 传输边界在 `wechatpad.py` 展开 v861 同步包的 `AddMsgs`，每个内部事件继续进入现有 `ws_message`；好友申请 XML 仍由 `wechatpad_friend_request.py` 解析，该模块把 `scene`、`V3`、`V4` 和可选的 `chatroomusername` 交给 `WeChatPadClient`，再由 `FriendApi` 映射为 v861 `VerifyUserRequestModel`。不新增第二个解析器或兼容入口。

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
- 外层 `Code=200` 继续兼容；响应存在 v861 的 `Data.base_response.ret` 时，只有 `ret=0` 视为业务成功。
- v861 WebSocket 返回带 `AddMsgs` 的同步包时逐条派发内部字典；原有单条事件载荷仍直接派发。
- `/friend/AgreeAdd` 保持 `OpCode=3`；Swagger 中该值明确定义为“同意好友/通过好友验证”，示例值 `2` 仅用于“添加好友/发送验证申请”。
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
- Fact：重放旧申请得到外层 `Code=200`、内层 `Data.base_response.ret=-24`，旧票据已经失效。
- Fact：生产 AOF 中 2026-08-03 10:20:25 的真实申请位于 `AddMsgs`，`scene=14`，群 ID、V3、V4 均存在；同期 LangBot 好友申请处理日志为 0。
- Fact：生产 WebSocket 在该申请时间窗口保持 `101` 连接并持续传输数据，当前 `on_message` 却把整个同步包当作单条事件提交，顶层不含 `msg_type`，因此被消息守卫忽略。
- Unknown：新鲜好友申请的成功响应尚待下一条真实申请验证。

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
- [x] 新增 `Data.base_response.ret != 0` 测试，期望记录失败而不是成功。
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

## Task 3: 展开 WebSocket 同步包并恢复真实事件派发

**Files:** modify `src/langbot/pkg/platform/sources/wechatpad.py`, `tests/unit_tests/platform/test_wechatpad.py`

**Why:** v861 `/ws/GetSyncMsg` 返回的顶层对象是同步包，真实好友申请位于 `AddMsgs`。当前代码把同步包直接交给只识别单条消息的守卫，导致所有内部事件在进入自动同意处理器前被忽略。

**Impact/Compatibility:** 只在 WebSocket 传输边界展开同步包；已有单条字典载荷保持原派发方式，空或非法 `AddMsgs` 不产生伪事件。`ws_message`、好友申请解析和 API 请求合同不变。

**Verification:** 先运行 `-k sync_envelope` 观察测试因缺少 `_submit_ws_payload` 失败，再运行完整 WeChatPad 测试与 Ruff，预期退出码均为 `0`。

- [x] 在 `test_wechatpad.py` 新增 `test_sync_envelope_submits_each_add_msg_to_main_loop`，构造包含 `msg_type=37` 与 `msg_type=1` 的 `AddMsgs`，断言两条内部事件按顺序提交。
- [x] 运行 `D:\code\LangBot\.venv\Scripts\python.exe -m pytest tests\unit_tests\platform\test_wechatpad.py -k sync_envelope -q`，记录因 `_submit_ws_payload` 尚不存在而失败的 RED。
- [x] 在 `WeChatPadAdapter` 增加 `_submit_ws_payload`：`AddMsgs` 为列表时逐条调用 `_submit_ws_message`，否则保持原单条派发；让 WebSocket `on_message` 调用该方法。
- [x] 运行完整 `test_wechatpad.py` 和计划顶部 Ruff 命令，确认同步包、单条载荷及既有好友申请合同全部通过。
- [ ] 提交 `fix(wechatpad): dispatch messages from sync envelopes`，推送并仅重建生产 `langbot`。

### Repair Track

- Root cause：WebSocket 传输层未展开 v861 同步包，`AddMsgs` 中的真实事件从未进入消息守卫与好友申请处理器。
- Canonical owner：`WeChatPadAdapter` 的 WebSocket `on_message` 边界。
- Minimal change：新增一个同步包派发方法并替换一处直接提交调用。
- Verification：同步包单测、完整适配器回归、生产新申请日志与好友关系确认。

### Retirement Track

- Retired logic：把所有 WebSocket JSON 都视为单条消息的直接提交路径。
- Retained boundary：不含列表型 `AddMsgs` 的单条事件仍按原路径派发。
- Retirement trigger：本次提交立即替换直接提交，不保留并行 owner 或 fallback。

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
